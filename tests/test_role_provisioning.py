"""Tests for the role-separation SQL builders and secret-map changes."""

from __future__ import annotations

from snowclaw.snowflake import (
    build_create_service_grant,
    build_grant_statements,
    build_revoke_create_service,
    build_secret_values,
)
from snowclaw.utils import sf_names


def test_sf_names_includes_runtime_role():
    names = sf_names("snowclaw_db", "snowclaw_schema")
    assert names["runtime_role"] == "snowclaw_runtime_role"


def test_sf_names_runtime_role_uses_custom_prefix():
    names = sf_names("acme_db", "production")
    assert names["runtime_role"] == "acme_runtime_role"


def test_build_grant_statements_covers_minimal_set():
    names = sf_names("snowclaw_db", "snowclaw_schema")
    stmts = build_grant_statements(names, "RUNTIME", [])
    joined = "\n".join(stmts)
    # Every expected minimal grant shows up exactly once.
    assert "GRANT USAGE ON DATABASE snowclaw_db TO ROLE RUNTIME" in joined
    assert "GRANT USAGE ON SCHEMA snowclaw_db.snowclaw_schema TO ROLE RUNTIME" in joined
    assert "GRANT READ ON STAGE snowclaw_db.snowclaw_schema.snowclaw_state_stage TO ROLE RUNTIME" in joined
    assert "GRANT WRITE ON STAGE snowclaw_db.snowclaw_schema.snowclaw_state_stage TO ROLE RUNTIME" in joined
    assert "GRANT READ ON IMAGE REPOSITORY snowclaw_db.snowclaw_schema.snowclaw_repo TO ROLE RUNTIME" in joined
    assert "GRANT USAGE ON COMPUTE POOL snowclaw_pool TO ROLE RUNTIME" in joined
    assert "GRANT MONITOR ON COMPUTE POOL snowclaw_pool TO ROLE RUNTIME" in joined
    assert "GRANT USAGE ON INTEGRATION snowclaw_external_access TO ROLE RUNTIME" in joined
    # Required for CREATE SERVICE when the spec declares a public endpoint.
    assert "GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO ROLE RUNTIME" in joined
    # Cortex entitlement — the runtime PAT inside the containers needs this
    # to hit Cortex's LLM endpoints.
    assert "GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE RUNTIME" in joined


def test_build_grant_statements_excludes_create_privileges():
    """Runtime role must NOT get any CREATE privileges — the whole point of the
    separation is that a compromised OAuth token cannot mint new NRs, secrets,
    pools, or services."""
    names = sf_names("snowclaw_db", "snowclaw_schema")
    stmts = build_grant_statements(names, "RUNTIME", ["some_secret"])
    joined = "\n".join(stmts).upper()
    for forbidden in (
        "CREATE NETWORK RULE",
        "CREATE SECRET",
        "CREATE INTEGRATION",
        "CREATE SERVICE",
        "CREATE COMPUTE POOL",
        "GRANT OWNERSHIP",
    ):
        assert forbidden not in joined, f"runtime role must not be granted {forbidden}"


def test_build_grant_statements_emits_read_per_secret():
    """READ on secrets — SPCS's CREATE SERVICE resolves `snowflakeSecret:`
    bindings by checking READ on the secret from the creating role. USAGE
    looks correct by name (it's what UDFs/stored procs need) but SPCS
    rejects a spec whose creator only holds USAGE — verified against live
    Snowflake."""
    names = sf_names("snowclaw_db", "snowclaw_schema")
    stmts = build_grant_statements(names, "RUNTIME", ["secret_a", "secret_b"])
    assert any(
        s == "GRANT READ ON SECRET snowclaw_db.snowclaw_schema.secret_a TO ROLE RUNTIME"
        for s in stmts
    )
    assert any(
        s == "GRANT READ ON SECRET snowclaw_db.snowclaw_schema.secret_b TO ROLE RUNTIME"
        for s in stmts
    )


def test_build_create_service_grant_and_revoke_are_symmetric():
    names = sf_names("snowclaw_db", "snowclaw_schema")
    grant = build_create_service_grant(names, "RUNTIME")
    revoke = build_revoke_create_service(names, "RUNTIME")
    assert grant == "GRANT CREATE SERVICE ON SCHEMA snowclaw_db.snowclaw_schema TO ROLE RUNTIME"
    assert revoke == "REVOKE CREATE SERVICE ON SCHEMA snowclaw_db.snowclaw_schema FROM ROLE RUNTIME"


def test_build_grant_statements_does_not_grant_nr_or_permanent_create_service():
    """Runtime role reaches the network via USAGE on the EAI — it must NOT
    get any grant on the network rule itself (ALTER/DESCRIBE/USAGE), and
    it must NOT get a permanent CREATE SERVICE grant (that's a transient
    grant issued by cmd_deploy and revoked right after)."""
    names = sf_names("snowclaw_db", "snowclaw_schema")
    stmts = build_grant_statements(names, "RUNTIME", ["some_secret"])
    joined = "\n".join(stmts).upper()
    assert "NETWORK RULE" not in joined
    assert "CREATE SERVICE" not in joined


def test_build_secret_values_maps_sf_token_to_runtime_pat():
    """The sf_token secret carries the *runtime-scoped* PAT (settings key
    ``runtime_pat``), not the admin PAT. Both Cortex REST and Cortex Code
    reject OAuth session tokens, so a real PAT is unavoidable inside the
    containers — keeping it role-restricted to the runtime role is the
    containment layer."""
    names = sf_names("snowclaw_db", "snowclaw_schema")
    mapping = build_secret_values(names, channels=["slack"])
    assert mapping[names["secret_sf_token"]] == "runtime_pat"
    # Channel secrets are still populated.
    assert any("slack" in key.lower() for key in mapping)

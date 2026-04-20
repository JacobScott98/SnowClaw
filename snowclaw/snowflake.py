"""Snowflake object creation via REST API."""

from __future__ import annotations

import re

import requests

from snowclaw.network import NetworkRule, apply_network_rules, get_channel_secrets
from snowclaw.utils import console, sf_names, sf_proxy_names, snowflake_rest_execute


# ---------------------------------------------------------------------------
# Role separation — runtime role creation, minimal grants, ownership transfer
# ---------------------------------------------------------------------------


def build_grant_statements(
    names: dict,
    runtime_role: str,
    secret_names: list[str],
) -> list[str]:
    """Minimal USAGE/READ grants on everything the runtime service needs.

    Deliberately excluded: any CREATE privileges beyond the one-time
    ``CREATE SERVICE`` grant issued transiently by ``cmd_deploy`` (and
    revoked right after), ownership on anything other than the service
    (acquired by creating it as the runtime role), USAGE on other compute
    pools/warehouses, and any grant on the network rule itself — runtime
    reaches the network via the EAI, which is enough.
    """
    s = names["schema"]
    stmts = [
        f"GRANT USAGE ON DATABASE {names['db']} TO ROLE {runtime_role}",
        f"GRANT USAGE ON SCHEMA {s} TO ROLE {runtime_role}",
        f"GRANT READ ON STAGE {s}.{names['stage']} TO ROLE {runtime_role}",
        f"GRANT WRITE ON STAGE {s}.{names['stage']} TO ROLE {runtime_role}",
        f"GRANT READ ON IMAGE REPOSITORY {s}.{names['repo']} TO ROLE {runtime_role}",
        f"GRANT USAGE ON COMPUTE POOL {names['pool']} TO ROLE {runtime_role}",
        f"GRANT MONITOR ON COMPUTE POOL {names['pool']} TO ROLE {runtime_role}",
        f"GRANT USAGE ON INTEGRATION {names['external_access']} TO ROLE {runtime_role}",
        # Required for CREATE SERVICE when the spec declares a public
        # endpoint (OpenClaw's 18789 gateway). Without this the runtime
        # role's CREATE SERVICE fails with "Access denied. Insufficient
        # privileges. Please grant BIND SERVICE ENDPOINT to service owner
        # role."
        f"GRANT BIND SERVICE ENDPOINT ON ACCOUNT TO ROLE {runtime_role}",
        # Cortex access — the runtime-scoped PAT inside the containers
        # authenticates as this role, so the role needs Cortex entitlement
        # for the LLM REST endpoints to work.
        f"GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE {runtime_role}",
    ]
    for secret in secret_names:
        # secret name may already be fully-qualified or bare — normalize
        fqn = secret if "." in secret else f"{s}.{secret}"
        # READ — this is the privilege SPCS actually checks when resolving
        # `snowflakeSecret:` bindings in a service spec at CREATE SERVICE
        # time. USAGE looks right by name (it's the "normal" secret-use
        # privilege for UDFs/stored procs) but SPCS rejects a spec whose
        # owning role only holds USAGE: verified empirically against
        # Snowflake's SPCS implementation. See test4 repro under
        # tests/test_role_provisioning.py.
        stmts.append(f"GRANT READ ON SECRET {fqn} TO ROLE {runtime_role}")
    return stmts


def build_create_service_grant(names: dict, runtime_role: str) -> str:
    """One-shot grant that lets the runtime role call ``CREATE SERVICE``.

    Issued by ``cmd_deploy`` just before service creation and revoked
    immediately after via :func:`build_revoke_create_service`. We need this
    because Snowflake blocks ``GRANT OWNERSHIP ON SERVICE``, so the only
    way for the runtime role to end up as the service owner is to create
    the service itself. Leaving ``CREATE SERVICE`` granted permanently
    would let a compromised runtime spin up sibling services.
    """
    return (
        f"GRANT CREATE SERVICE ON SCHEMA {names['schema']} "
        f"TO ROLE {runtime_role}"
    )


def build_revoke_create_service(names: dict, runtime_role: str) -> str:
    """Revoke the transient ``CREATE SERVICE`` privilege post-deploy."""
    return (
        f"REVOKE CREATE SERVICE ON SCHEMA {names['schema']} "
        f"FROM ROLE {runtime_role}"
    )


def role_exists(account: str, pat: str, role: str, admin_role: str) -> bool:
    """Return True if ``role`` exists in the account."""
    sql = f"SHOW ROLES LIKE '{role}'"
    result = snowflake_rest_execute(account, pat, sql, role=admin_role)
    return bool(result.get("data"))


def validate_pat_role_restriction(
    account: str, pat: str, admin_role: str
) -> tuple[bool, list[str]]:
    """Best-effort check that the admin PAT is restricted to ``admin_role``.

    Returns ``(is_restricted, available_roles)``. Restricted means the PAT
    cannot assume any role other than ``admin_role``. If CURRENT_AVAILABLE_ROLES
    returns multiple roles, the PAT has no role restriction and the user
    should be warned.
    """
    sql = "SELECT CURRENT_AVAILABLE_ROLES()"
    try:
        result = snowflake_rest_execute(account, pat, sql, role=admin_role)
    except requests.HTTPError:
        return (False, [])
    data = result.get("data") or []
    if not data or not data[0]:
        return (False, [])
    raw = data[0][0]
    try:
        import json as _json
        roles = _json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        return (False, [])
    roles_upper = [r.upper() for r in roles]
    restricted = len(roles_upper) == 1 and roles_upper[0] == admin_role.upper()
    return (restricted, roles)


def build_setup_statements(names: dict) -> list[str]:
    """Build SQL statements to create non-secret Snowflake objects from derived names.

    Network rules and external access integrations are managed separately
    via snowclaw.network — they are not included here.
    """
    s = names["schema"]  # fully qualified: db.schema
    return [
        f"CREATE DATABASE IF NOT EXISTS {names['db']}",
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"CREATE IMAGE REPOSITORY IF NOT EXISTS {s}.{names['repo']}",
        (
            f"CREATE STAGE IF NOT EXISTS {s}.{names['stage']} "
            "ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE') DIRECTORY = (ENABLE = TRUE)"
        ),
        (
            f"CREATE COMPUTE POOL IF NOT EXISTS {names['pool']} "
            "MIN_NODES = 1 MAX_NODES = 1 INSTANCE_FAMILY = CPU_X64_S"
        ),
    ]


def build_secret_values(names: dict, channels: list[str]) -> dict[str, str]:
    """Map secret object names to settings keys.

    ``sf_token`` holds the *runtime-scoped* PAT (``settings['runtime_pat']``),
    not the admin PAT. The admin PAT stays on the user's laptop; the runtime
    PAT is what lives inside both containers for Cortex Code, snowsql, and
    the Cortex proxy. Cortex REST and Cortex Code both reject OAuth session
    tokens, so a real PAT inside the containers is unavoidable — keeping it
    role-restricted to the runtime role is what makes that safe.
    """
    mapping: dict[str, str] = {
        names["secret_sf_token"]: "runtime_pat",
    }
    # Add channel secrets dynamically from the registry
    prefix = names["prefix"]
    for sec in get_channel_secrets(prefix, channels):
        # settings key is the env_var name (e.g. SLACK_BOT_TOKEN)
        mapping[sec["secret_name"]] = sec["env_var"]
    return mapping


_LABEL_RE = re.compile(
    r"CREATE\s+\w+(?:\s+\w+)*\s+(?:IF NOT EXISTS\s+)?([\w.]+)", re.IGNORECASE
)


def run_snowflake_setup(settings: dict) -> list[str]:
    """Create Snowflake objects via REST API.

    The runtime role must already exist (SnowClaw does not provision roles
    on the user's behalf). Runtime-role USAGE/READ grants are emitted by
    :func:`apply_runtime_grants` after network rules are applied — the EAI
    must exist before it can be granted.

    Returns the list of bare secret names created (used by the caller to
    grant READ on them to the runtime role).
    """
    account = settings["account"]
    pat = settings["pat"]
    admin_role = settings["admin_role"]
    database = settings.get("database", "snowclaw_db")
    schema = settings.get("schema", "snowclaw_schema")
    channels = settings.get("channels", [])
    names = sf_names(database, schema)
    statements = build_setup_statements(names)
    secret_values = build_secret_values(names, channels)

    # Build secret statements separately with real values (no string-matching)
    s = names["schema"]
    secret_stmts: list[tuple[str, str]] = []
    created_secret_names: list[str] = []
    for secret_name, settings_key in secret_values.items():
        value = settings.get(settings_key, "")
        escaped = value.replace("'", "\\'") if value else ""
        stmt = (
            f"CREATE OR REPLACE SECRET {s}.{secret_name} "
            f"TYPE = GENERIC_STRING SECRET_STRING = '{escaped}'"
        )
        secret_stmts.append((stmt, f"{s}.{secret_name}"))
        created_secret_names.append(secret_name)

    # Tool credential secrets
    tool_credentials = settings.get("tool_credentials", {})
    tool_secret_map = {
        "GH_TOKEN": names["secret_gh_token"],
        "BRAVE_API_KEY": names["secret_brave_api_key"],
    }
    for env_var, secret_name in tool_secret_map.items():
        value = tool_credentials.get(env_var, "")
        if value:
            escaped = value.replace("'", "\\'")
            stmt = (
                f"CREATE OR REPLACE SECRET {s}.{secret_name} "
                f"TYPE = GENERIC_STRING SECRET_STRING = '{escaped}'"
            )
            secret_stmts.append((stmt, f"{s}.{secret_name}"))
            created_secret_names.append(secret_name)

    def _execute(stmt: str, label: str | None = None):
        if not label:
            m = _LABEL_RE.search(stmt)
            label = m.group(1) if m else stmt[:60]
        try:
            snowflake_rest_execute(account, pat, stmt, role=admin_role)
            console.print(f"  [green]✓[/green] {label}")
        except requests.HTTPError as e:
            console.print(f"  [red]✗[/red] Failed: {stmt[:80]}...")
            console.print(f"    [dim]{e}[/dim]")
            raise

    with console.status("[bold cyan]Creating Snowflake objects..."):
        # Create non-secret objects
        for stmt in statements:
            _execute(stmt)
        # Create secrets with actual values
        for stmt, label in secret_stmts:
            _execute(stmt, label)

    return created_secret_names


def apply_runtime_grants(
    account: str,
    pat: str,
    admin_role: str,
    runtime_role: str,
    names: dict,
    secret_names: list[str],
) -> bool:
    """Grant minimal USAGE/READ privileges to the runtime role.

    Must be called after :func:`run_snowflake_setup` and after
    :func:`snowclaw.network.apply_network_rules` — the EAI referenced in
    the grants must exist first. Idempotent.
    """
    statements = build_grant_statements(names, runtime_role, secret_names)
    with console.status("[bold cyan]Applying runtime-role grants..."):
        for stmt in statements:
            try:
                snowflake_rest_execute(account, pat, stmt, role=admin_role)
                m = _LABEL_RE.search(stmt) if stmt.startswith("CREATE") else None
                label = m.group(1) if m else stmt[:72]
                console.print(f"  [green]✓[/green] {label}")
            except requests.HTTPError as e:
                console.print(f"  [red]✗[/red] Failed: {stmt[:80]}")
                console.print(f"    [dim]{e}[/dim]")
                return False
    return True


# ---------------------------------------------------------------------------
# Standalone proxy setup
# ---------------------------------------------------------------------------


def build_proxy_setup_statements(names: dict) -> list[str]:
    """Build SQL statements for standalone proxy Snowflake objects.

    No secrets needed — each user passes their own PAT via the
    X-Cortex-Token header which survives SPCS ingress stripping.
    """
    s = names["schema"]
    return [
        f"CREATE DATABASE IF NOT EXISTS {names['db']}",
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"CREATE IMAGE REPOSITORY IF NOT EXISTS {s}.{names['repo']}",
        (
            f"CREATE COMPUTE POOL IF NOT EXISTS {names['pool']} "
            "MIN_NODES = 1 MAX_NODES = 1 INSTANCE_FAMILY = CPU_X64_XS"
        ),
    ]


def run_proxy_snowflake_setup(settings: dict):
    """Create Snowflake objects for standalone proxy deployment."""
    account = settings["account"]
    pat = settings["pat"]
    database = settings.get("database", "snowclaw_db")
    schema = settings.get("schema", "snowclaw_schema")
    names = sf_proxy_names(database, schema)
    statements = build_proxy_setup_statements(names)

    def _execute(stmt: str, label: str | None = None):
        if not label:
            m = _LABEL_RE.search(stmt)
            label = m.group(1) if m else stmt[:60]
        try:
            snowflake_rest_execute(account, pat, stmt)
            console.print(f"  [green]✓[/green] {label}")
        except requests.HTTPError as e:
            console.print(f"  [red]✗[/red] Failed: {stmt[:80]}...")
            console.print(f"    [dim]{e}[/dim]")
            raise

    with console.status("[bold cyan]Creating Snowflake objects..."):
        for stmt in statements:
            _execute(stmt)

    # Create network rule for Cortex access
    cortex_rule = NetworkRule(host="*.snowflakecomputing.com", port=443, reason="Cortex API")
    apply_network_rules(account, pat, names, [cortex_rule])

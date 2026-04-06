"""Snowflake object creation via REST API."""

from __future__ import annotations

import re

import requests

from snowclaw.network import NetworkRule, apply_network_rules, get_channel_secrets
from snowclaw.utils import console, sf_names, sf_proxy_names, snowflake_rest_execute


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

    Includes infrastructure secrets (sf_token) plus all secret
    credentials for the given enabled channels.
    """
    mapping = {
        names["secret_sf_token"]: "pat",
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


def run_snowflake_setup(settings: dict):
    """Create Snowflake objects via REST API."""
    account = settings["account"]
    pat = settings["pat"]
    database = settings.get("database", "snowclaw_db")
    schema = settings.get("schema", "snowclaw_schema")
    channels = settings.get("channels", [])
    names = sf_names(database, schema)
    statements = build_setup_statements(names)
    secret_values = build_secret_values(names, channels)

    # Build secret statements separately with real values (no string-matching)
    s = names["schema"]
    secret_stmts: list[tuple[str, str]] = []
    for secret_name, settings_key in secret_values.items():
        value = settings.get(settings_key, "")
        escaped = value.replace("'", "\\'") if value else ""
        stmt = (
            f"CREATE OR REPLACE SECRET {s}.{secret_name} "
            f"TYPE = GENERIC_STRING SECRET_STRING = '{escaped}'"
        )
        secret_stmts.append((stmt, f"{s}.{secret_name}"))

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
        # Create non-secret objects
        for stmt in statements:
            _execute(stmt)
        # Create secrets with actual values
        for stmt, label in secret_stmts:
            _execute(stmt, label)


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

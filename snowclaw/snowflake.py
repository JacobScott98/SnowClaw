"""Snowflake object creation via REST API."""

from __future__ import annotations

import re

import requests

from snowclaw.utils import console, sf_names, snowflake_rest_execute


def build_setup_statements(names: dict) -> list[str]:
    """Build SQL statements to create non-secret Snowflake objects from derived names."""
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
            f"CREATE OR REPLACE NETWORK RULE {s}.{names['egress_rule']} "
            "MODE = EGRESS TYPE = HOST_PORT VALUE_LIST = ("
            "'openrouter.ai:443', 'api.slack.com:443', "
            "'wss-primary.slack.com:443', 'wss-backup.slack.com:443', "
            "'*.snowflakecomputing.com:443')"
        ),
        (
            f"CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION {names['external_access']} "
            f"ALLOWED_NETWORK_RULES = ({s}.{names['egress_rule']}) "
            "ENABLED = TRUE"
        ),
        (
            f"CREATE COMPUTE POOL IF NOT EXISTS {names['pool']} "
            "MIN_NODES = 1 MAX_NODES = 1 INSTANCE_FAMILY = CPU_X64_S"
        ),
    ]


def build_secret_values(names: dict) -> dict[str, str]:
    """Map secret object names to settings keys."""
    return {
        names["secret_sf_token"]: "pat",
        names["secret_openrouter_key"]: "openrouter_key",
        names["secret_slack_bot_token"]: "slack_bot_token",
        names["secret_slack_app_token"]: "slack_app_token",
    }


_LABEL_RE = re.compile(
    r"CREATE\s+\w+(?:\s+\w+)*\s+(?:IF NOT EXISTS\s+)?([\w.]+)", re.IGNORECASE
)


def run_snowflake_setup(settings: dict):
    """Create Snowflake objects via REST API."""
    account = settings["account"]
    pat = settings["pat"]
    prefix = settings.get("prefix", "snowclaw")
    names = sf_names(prefix)
    statements = build_setup_statements(names)
    secret_values = build_secret_values(names)

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

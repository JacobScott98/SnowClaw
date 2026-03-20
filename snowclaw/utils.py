"""Snowflake naming, project discovery, and shared utilities."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from snowclaw import __version__

console = Console()


# ---------------------------------------------------------------------------
# Snowflake naming
# ---------------------------------------------------------------------------

def sf_names(database: str, schema: str) -> dict:
    """Derive all Snowflake object names from a database and schema."""
    prefix = re.sub(r"_db$", "", database.lower())
    return {
        "db": database,
        "schema": f"{database}.{schema}",
        "schema_name": schema,
        "repo": f"{prefix}_repo",
        "stage": f"{prefix}_state_stage",
        "egress_rule": f"{prefix}_egress_rule",
        "external_access": f"{prefix}_external_access",
        "pool": f"{prefix}_pool",
        "service": f"{prefix}_service",
        "secret_sf_token": f"{prefix}_sf_token",
        "secret_slack_bot_token": f"{prefix}_slack_bot_token",
        "secret_slack_app_token": f"{prefix}_slack_app_token",
    }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def get_templates_dir() -> Path:
    """Return the path to the templates/ directory bundled with the CLI."""
    return Path(__file__).resolve().parent.parent / "templates"


def find_project_root() -> Path:
    """Walk up from cwd to find the SnowClaw project root (contains .snowclaw marker)."""
    cur = Path.cwd()
    for d in [cur, *cur.parents]:
        if (d / ".snowclaw").exists():
            return d
    console.print("[red]Could not find SnowClaw project root (no .snowclaw marker found).[/red]")
    console.print("Run [cyan]snowclaw setup[/cyan] in a fresh directory to create a project.")
    sys.exit(1)


def read_marker(root: Path) -> dict:
    """Read the .snowclaw marker. Handles both old (file) and new (directory) formats."""
    marker_path = root / ".snowclaw"
    if marker_path.is_file():
        # Old format: .snowclaw is a JSON file
        return json.loads(marker_path.read_text())
    config_path = marker_path / "config.json"
    if config_path.exists():
        return json.loads(config_path.read_text())
    return {}


def write_marker(root: Path, data: dict):
    """Write the .snowclaw/config.json marker file."""
    marker_dir = root / ".snowclaw"
    # Migrate from old file format if needed
    if marker_dir.is_file():
        marker_dir.unlink()
    marker_dir.mkdir(exist_ok=True)
    config_path = marker_dir / "config.json"
    config_path.write_text(json.dumps(data, indent=2) + "\n")


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Ignores comments and blank lines."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def load_connections_toml(path: Path) -> dict[str, str]:
    """Read the [main] section from connections.toml."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return dict(data.get("main", {}))


def snowflake_rest_execute(
    account: str,
    pat: str,
    sql: str,
    database: str | None = None,
    schema: str | None = None,
    warehouse: str | None = None,
    role: str | None = None,
) -> dict:
    """Execute a single SQL statement via the Snowflake REST API."""
    url = f"https://{account}.snowflakecomputing.com/api/v2/statements"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
    }
    body: dict = {"statement": sql, "timeout": 60}
    if database:
        body["database"] = database.upper()
    if schema:
        body["schema"] = schema.upper()
    if warehouse:
        body["warehouse"] = warehouse.upper()
    if role:
        body["role"] = role.upper()

    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if not resp.ok:
        # Include response body in error for debugging
        detail = resp.text[:500] if resp.text else "(no body)"
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} for url: {resp.url}\n{detail}",
            response=resp,
        )
    return resp.json()


def load_snowflake_context(root: Path) -> dict:
    """Load Snowflake connection context from marker + .env + connections.toml.

    Returns dict with: account, token, user, registry_account, database, schema,
    warehouse, names (from sf_names), and the raw env/conn dicts.
    """
    import os as _os

    marker = read_marker(root)
    env = {**_os.environ, **load_dotenv(root / ".env")}
    conn = load_connections_toml(root / "connections.toml")

    database = marker.get("database", env.get("SNOWCLAW_DB", "snowclaw_db"))
    schema = marker.get("schema", env.get("SNOWCLAW_SCHEMA", "snowclaw_schema"))
    names = sf_names(database, schema)

    account = env.get("SNOWFLAKE_ACCOUNT")
    token = env.get("SNOWFLAKE_TOKEN")
    registry_account = env.get("SNOWFLAKE_REGISTRY_ACCOUNT")
    sf_user = env.get("SNOWFLAKE_USER")
    warehouse = env.get("SNOWFLAKE_WAREHOUSE") or conn.get("warehouse")

    return {
        "account": account,
        "token": token,
        "user": sf_user,
        "registry_account": registry_account,
        "database": database,
        "schema": schema,
        "warehouse": warehouse,
        "names": names,
        "env": env,
        "conn": conn,
        "marker": marker,
    }


def render_banner():
    """Print the SnowClaw welcome banner."""
    title = Text("❄  SnowClaw", style="bold cyan")
    subtitle = Text(f"v{__version__} — OpenClaw on Snowflake", style="dim")
    banner = Text.assemble(title, "\n", subtitle)
    console.print(Panel(banner, expand=False, border_style="cyan"))
    console.print()

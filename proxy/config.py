"""Configuration for the Cortex proxy."""

import os
import tomllib
from pathlib import Path


def _account_from_connections_toml() -> str | None:
    """Read the Snowflake account from ~/.snowflake/connections.toml."""
    path = Path.home() / ".snowflake" / "connections.toml"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        # Try default connection first, then first available
        conn = data.get("default", next(iter(data.values()), {}))
        if isinstance(conn, dict):
            return conn.get("account")
    except Exception:
        return None
    return None


def get_cortex_base_url() -> str:
    """Return the Cortex base URL from env or connections.toml."""
    url = os.environ.get("CORTEX_BASE_URL")
    if url:
        return url.rstrip("/")

    account = _account_from_connections_toml()
    if account:
        return f"https://{account}.snowflakecomputing.com/api/v2/cortex/v1"

    raise RuntimeError(
        "CORTEX_BASE_URL not set and no account found in ~/.snowflake/connections.toml"
    )


def get_proxy_port() -> int:
    return int(os.environ.get("PROXY_PORT", "8080"))


def is_claude_model(model: str) -> bool:
    return model.lower().startswith("claude")


def is_response_logging_enabled() -> bool:
    """Check if detailed Cortex response metadata logging is enabled.

    Set PROXY_LOG_RESPONSES=1 (or "true"/"yes") to enable.
    """
    val = os.environ.get("PROXY_LOG_RESPONSES", "").lower()
    return val in ("1", "true", "yes")

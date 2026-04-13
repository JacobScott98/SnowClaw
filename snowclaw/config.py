"""Config file writers (.env, openclaw.json, connections.toml)."""

from __future__ import annotations

import json
from pathlib import Path

from snowclaw.network import CHANNEL_REGISTRY, TOOL_REGISTRY
from snowclaw.utils import console

DEFAULT_MAX_TOKENS = 131072

CORTEX_CLAUDE_CONTEXT_WINDOW = 1048576

CORTEX_CLAUDE_MODELS = [
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "contextWindow": CORTEX_CLAUDE_CONTEXT_WINDOW, "maxTokens": DEFAULT_MAX_TOKENS},
    {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "contextWindow": CORTEX_CLAUDE_CONTEXT_WINDOW, "maxTokens": DEFAULT_MAX_TOKENS},
]

CORTEX_OPENAI_MODELS = [
    {"id": "openai-gpt-5.1", "name": "GPT-5.1", "contextWindow": 1047576, "maxTokens": DEFAULT_MAX_TOKENS},
]

# Combined list — Claude first so the setup wizard's "(Recommended)" marker lands on Claude.
CORTEX_MODELS = CORTEX_CLAUDE_MODELS + CORTEX_OPENAI_MODELS


def provider_for_model(model_id: str) -> str:
    """Return the openclaw.json provider id that should serve a given model.

    Claude models are routed through `cortex-claude` (Anthropic Messages API) so
    OpenClaw's native anthropic transport can inject prompt-cache markers. All
    other models go through `cortex-openai` (OpenAI chat completions API).
    """
    return "cortex-claude" if model_id.startswith("claude") else "cortex-openai"


def migrate_openclaw_config(root: Path) -> bool:
    """Idempotently migrate an existing openclaw.json to the cortex-claude/cortex-openai split.

    Pre-existing projects have a single `cortex` provider (openai-completions) and
    `agents.defaults.model = "cortex/<id>"`. This function:

      1. Splits `cortex` → `cortex-openai` (openai-completions) + `cortex-claude`
         (anthropic-messages), preserving any user-added models in their original
         entries (Claude models go to cortex-claude, everything else to cortex-openai).
      2. Rewrites `agents.defaults.model` and any per-agent `agents.<name>.model`
         that starts with `cortex/` to use the correct new prefix.
      3. Adds `agents.defaults.params.cacheRetention = "long"` if no params block exists.

    No-op if `cortex-claude` is already present (already migrated) or if no
    `openclaw.json` exists. Returns True iff a migration was performed.
    """
    config_path = root / "openclaw.json"
    if not config_path.exists():
        return False

    try:
        config = json.loads(config_path.read_text())
    except json.JSONDecodeError:
        return False

    providers = config.get("models", {}).get("providers", {})

    # Already migrated — bail out fast.
    if "cortex-claude" in providers:
        return False

    # Nothing to migrate if there's no old `cortex` provider either.
    if "cortex" not in providers:
        return False

    old_cortex = providers.pop("cortex")
    old_models = old_cortex.get("models", []) or []

    # Split user's existing model list by id. Preserve the original entries verbatim
    # so any custom contextWindow / maxTokens overrides survive the migration.
    claude_models = [m for m in old_models if str(m.get("id", "")).startswith("claude")]
    other_models = [m for m in old_models if not str(m.get("id", "")).startswith("claude")]

    # Backfill maxTokens on any model that lacks it — older configs were written before
    # this field was standard, and without it OpenClaw falls back to a conservative cap
    # that cuts long Cortex responses short. User-set values are preserved.
    for model in (*claude_models, *other_models):
        if isinstance(model, dict):
            model.setdefault("maxTokens", DEFAULT_MAX_TOKENS)

    # If the old provider had no Claude entries (unusual but possible), seed with the
    # canonical Claude list so users still get the new endpoint.
    if not claude_models:
        claude_models = list(CORTEX_CLAUDE_MODELS)

    api_key = old_cortex.get("apiKey", "${SNOWFLAKE_TOKEN}")

    providers["cortex-openai"] = {
        "baseUrl": "http://localhost:8080/v1",
        "apiKey": api_key,
        "api": "openai-completions",
        "models": other_models or list(CORTEX_OPENAI_MODELS),
    }
    providers["cortex-claude"] = {
        "baseUrl": "http://localhost:8080",
        "apiKey": api_key,
        "api": "anthropic-messages",
        "models": claude_models,
        "headers": {"anthropic-version": "2023-06-01"},
    }

    # Re-attach in case the dict literal was created fresh somewhere upstream.
    config.setdefault("models", {})["providers"] = providers

    # Rewrite agent model references that pinned the old `cortex/` prefix.
    def _rewrite_model_ref(model_ref: str) -> str:
        if not isinstance(model_ref, str) or not model_ref.startswith("cortex/"):
            return model_ref
        model_id = model_ref.split("/", 1)[1]
        return f"{provider_for_model(model_id)}/{model_id}"

    agents = config.get("agents", {})
    defaults = agents.setdefault("defaults", {}) if isinstance(agents, dict) else {}
    if "model" in defaults:
        defaults["model"] = _rewrite_model_ref(defaults["model"])

    # Forward-compat: enable cache retention so OpenClaw passes the long-TTL knob through.
    params = defaults.setdefault("params", {})
    params.setdefault("cacheRetention", "long")

    # Per-agent overrides (anything other than `defaults`).
    if isinstance(agents, dict):
        for agent_name, agent_cfg in agents.items():
            if agent_name == "defaults" or not isinstance(agent_cfg, dict):
                continue
            if "model" in agent_cfg:
                agent_cfg["model"] = _rewrite_model_ref(agent_cfg["model"])

    config["agents"] = agents

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    console.print(
        "  [yellow]→[/yellow] Migrated [cyan]openclaw.json[/cyan]: "
        "[dim]cortex →[/dim] cortex-openai + cortex-claude "
        "[dim](Claude models now use prompt caching)[/dim]"
    )
    console.print(
        "  [dim]Note: if this project is already deployed, run "
        "`snowclaw push --config-only` then `snowclaw restart` to apply.[/dim]"
    )
    return True


def migrate_claude_context_window(root: Path) -> bool:
    """Upgrade Claude model contextWindow from 200K to 1M in existing configs.

    Claude 4.6 models on Cortex support a 1M context window. Projects created
    before this default was changed still have ``contextWindow: 200000``.  This
    migration bumps any Claude model that still carries the old 200K value to
    the current ``CORTEX_CLAUDE_CONTEXT_WINDOW`` (1048576).  User-customised
    values (anything other than 200000) are left untouched.

    Returns True iff a change was written.
    """
    config_path = root / "openclaw.json"
    if not config_path.exists():
        return False

    try:
        config = json.loads(config_path.read_text())
    except json.JSONDecodeError:
        return False

    providers = config.get("models", {}).get("providers", {})

    OLD_CONTEXT_WINDOW = 200000
    changed = False

    for provider_id in ("cortex-claude", "cortex"):
        provider = providers.get(provider_id)
        if not provider:
            continue
        for model in provider.get("models", []):
            if not isinstance(model, dict):
                continue
            if not str(model.get("id", "")).startswith("claude"):
                continue
            if model.get("contextWindow") == OLD_CONTEXT_WINDOW:
                model["contextWindow"] = CORTEX_CLAUDE_CONTEXT_WINDOW
                changed = True

    if not changed:
        return False

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    console.print(
        "  [yellow]→[/yellow] Migrated [cyan]openclaw.json[/cyan]: "
        f"[dim]Claude contextWindow 200K → {CORTEX_CLAUDE_CONTEXT_WINDOW // 1000}K[/dim]"
    )
    console.print(
        "  [dim]Note: if this project is already deployed, run "
        "`snowclaw push --config-only` then `snowclaw restart` to apply.[/dim]"
    )
    return True


def write_dotenv(root: Path, settings: dict):
    """Write .env file with all secrets."""
    # Preserve any existing extra vars before overwriting (vars not managed by setup)
    managed_keys: set[str] = set()  # populated below after building lines
    existing_extra: list[str] = []
    env_file = root / ".env"

    lines = [
        "# SnowClaw environment variables (generated by snowclaw setup)",
        f"SNOWCLAW_DB={settings['database']}",
        f"SNOWCLAW_SCHEMA={settings['schema']}",
        f"SNOWFLAKE_ACCOUNT={settings['account']}",
        f"SNOWFLAKE_USER={settings['sf_user']}",
        f"SNOWFLAKE_TOKEN={settings['pat']}",
    ]
    # Write env vars for all enabled channels
    for ch_key in settings.get("channels", []):
        entry = CHANNEL_REGISTRY.get(ch_key)
        if not entry:
            continue
        for cred in entry["credentials"]:
            if cred.get("inline"):
                continue
            value = settings.get(cred["env_var"], "")
            if value:
                lines.append(f"{cred['env_var']}={value}")

    # Tool credentials
    tool_credentials = settings.get("tool_credentials", {})
    for env_var, value in tool_credentials.items():
        if value:
            lines.append(f"{env_var}={value}")

    # Cortex proxy base URL
    lines.append(
        f"CORTEX_BASE_URL=https://{settings['account']}.snowflakecomputing.com/api/v2/cortex/v1"
    )

    # Collect keys managed by setup so we can preserve user-added extras
    managed_keys = {"SNOWCLAW_DB", "SNOWCLAW_SCHEMA", "SNOWFLAKE_ACCOUNT",
                    "SNOWFLAKE_USER", "SNOWFLAKE_TOKEN", "CORTEX_BASE_URL",
                    "SNOWCLAW_MASK_VARS"}
    for ch_key in settings.get("channels", []):
        entry = CHANNEL_REGISTRY.get(ch_key)
        if entry:
            for cred in entry["credentials"]:
                managed_keys.add(cred["env_var"])
    for env_var in tool_credentials:
        managed_keys.add(env_var)

    # Preserve extra vars the user added manually
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key not in managed_keys:
                    existing_extra.append(stripped)
    if existing_extra:
        lines.append("")
        for extra_line in existing_extra:
            lines.append(extra_line)

    # Auto-populate SNOWCLAW_MASK_VARS from all secret env vars
    mask_vars: list[str] = []
    # Add all env vars that have values (except known config vars)
    config_only = {"SNOWCLAW_DB", "SNOWCLAW_SCHEMA", "SNOWCLAW_MASK_VARS",
                   "CORTEX_BASE_URL", "IMAGE_TAG"}
    for line_str in lines:
        if line_str.startswith("#") or "=" not in line_str:
            continue
        key = line_str.partition("=")[0].strip()
        if key in config_only or key == "SNOWFLAKE_ACCOUNT" or key == "SNOWFLAKE_USER":
            continue
        value = line_str.partition("=")[2].strip()
        if value and key not in mask_vars:
            mask_vars.append(key)
    if mask_vars:
        lines.append(f"SNOWCLAW_MASK_VARS={','.join(mask_vars)}")

    (root / ".env").write_text("\n".join(lines) + "\n")
    console.print(f"  [green]✓[/green] Wrote {root / '.env'}")


def write_openclaw_config(root: Path, settings: dict):
    """Write openclaw.json with provider and channel config."""
    default_model_id = settings.get("default_model", CORTEX_MODELS[0]["id"])
    config: dict = {
        "gateway": {
            "auth": {
                "mode": "none",
            },
            "controlUi": {
                "dangerouslyAllowHostHeaderOriginFallback": True,
            },
        },
        "models": {"providers": {}},
        "channels": {},
        "agents": {"defaults": {
            "model": f"{provider_for_model(default_model_id)}/{default_model_id}",
            "params": {
                # Forward-compatible: OpenClaw injects ephemeral cache_control on system + trailing
                # user blocks. The "long" knob upgrades to 1h TTL only against api.anthropic.com /
                # Vertex hosts (OpenClaw gates this on hostname); against our proxy it still emits
                # 5m ephemeral, which is the v1 target. 1h upgrade is a future proxy enhancement.
                "cacheRetention": "long",
            },
        }},
    }

    # Cortex providers — split by API surface so Claude models can use OpenClaw's native
    # anthropic-messages transport (auto-injects cache_control markers) while OpenAI / Snowflake
    # models keep using the OpenAI chat-completions surface. The proxy serves both endpoints.
    config["models"]["providers"]["cortex-openai"] = {
        "baseUrl": "http://localhost:8080/v1",
        "apiKey": "${SNOWFLAKE_TOKEN}",
        "api": "openai-completions",
        "models": CORTEX_OPENAI_MODELS,
    }
    config["models"]["providers"]["cortex-claude"] = {
        # The Anthropic SDK appends `/v1/messages` to baseUrl, so the host root (no `/v1`) is
        # the correct value here. Final upstream URL hit by OpenClaw is http://localhost:8080/v1/messages.
        "baseUrl": "http://localhost:8080",
        "apiKey": "${SNOWFLAKE_TOKEN}",
        "api": "anthropic-messages",
        "models": CORTEX_CLAUDE_MODELS,
        "headers": {"anthropic-version": "2023-06-01"},
    }

    # Channels — generate config block for each enabled channel
    for ch_key in settings.get("channels", []):
        if ch_key == "slack":
            config["channels"]["slack"] = {
                "enabled": True,
                "mode": "socket",
                "accounts": {
                    "default": {
                        "botToken": "${SLACK_BOT_TOKEN}",
                        "appToken": "${SLACK_APP_TOKEN}",
                    }
                },
            }
        elif ch_key == "telegram":
            telegram_user_id = settings.get("TELEGRAM_USER_ID", "")
            config["channels"]["telegram"] = {
                "enabled": True,
                "botToken": "${TELEGRAM_BOT_TOKEN}",
                "dmPolicy": "allowlist",
                "allowFrom": [telegram_user_id] if telegram_user_id else [],
            }
        elif ch_key == "discord":
            discord_config: dict = {
                "enabled": True,
                "groupPolicy": "allowlist",
            }
            server_id = settings.get("DISCORD_SERVER_ID", "")
            user_id = settings.get("DISCORD_USER_ID", "")
            if server_id:
                guild_config: dict = {"requireMention": True}
                if user_id:
                    guild_config["users"] = [user_id]
                discord_config["guilds"] = {server_id: guild_config}
            config["channels"]["discord"] = discord_config

    # Brave Search tool (optional)
    if 'brave_search' in settings.get('tools', []):
        config['tools'] = {
            'web': {
                'search': {
                    'provider': 'brave',
                }
            }
        }

    # Plugins — generate section from .snowclaw/plugins.json
    from snowclaw.plugins import load_plugins

    configured_plugins = load_plugins(root)
    if configured_plugins:
        path_plugins = [p for p in configured_plugins if p["source"] == "path"]
        plugin_entries = {}
        for p in configured_plugins:
            plugin_entries[p["id"]] = {"enabled": True}
        plugins_section: dict = {"enabled": True, "entries": plugin_entries}
        if path_plugins:
            plugins_section["load"] = {"paths": ["/opt/snowclaw/plugins"]}
        config["plugins"] = plugins_section

    config_path = root / "openclaw.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    console.print(f"  [green]✓[/green] Wrote {config_path}")


def write_connections_toml(root: Path, settings: dict):
    """Write connections.toml for Snowflake connectivity."""
    content = f"""default_connection_name = "main"

[main]
account = "{settings['account']}"
user = "{settings['sf_user']}"
authenticator = "PROGRAMMATIC_ACCESS_TOKEN"
token = "{settings['pat']}"
warehouse = "{settings.get('warehouse', 'COMPUTE_WH')}"
role = "{settings.get('role', 'SYSADMIN')}"
"""
    conn_path = root / "connections.toml"
    conn_path.write_text(content)
    console.print(f"  [green]✓[/green] Wrote {conn_path}")

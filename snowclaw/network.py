"""Network rule management for SPCS external access."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from rich.table import Table

from snowclaw.utils import console, load_snowflake_context, snowflake_rest_execute


@dataclass(frozen=True)
class NetworkRule:
    """A single host:port egress rule."""

    host: str
    port: int = 443
    reason: str = ""

    @property
    def host_port(self) -> str:
        return f"{self.host}:{self.port}"

    def __eq__(self, other):
        if isinstance(other, NetworkRule):
            return self.host == other.host and self.port == other.port
        return NotImplemented

    def __hash__(self):
        return hash((self.host, self.port))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

RULES_FILE = "network-rules.json"

# Snowflake's documented "allow all outbound" pattern for MODE=EGRESS TYPE=HOST_PORT
# rules. `0.0.0.0` is the catch-all host; only 443 and 80 are accepted as ports.
# https://docs.snowflake.com/en/user-guide/network-rules
ALLOW_ALL_VALUE_LIST: tuple[str, ...] = ("0.0.0.0:443", "0.0.0.0:80")


@dataclass
class NetworkRulesConfig:
    """On-disk shape of ``.snowclaw/network-rules.json``.

    ``rules`` is preserved even when ``allow_all_egress`` is True so toggling
    back to restrict mode restores the user's prior allowlist.
    """

    allow_all_egress: bool = False
    rules: list[NetworkRule] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.rules is None:
            self.rules = []


def _rules_path(root: Path) -> Path:
    return root / ".snowclaw" / RULES_FILE


def load_network_config(root: Path) -> NetworkRulesConfig:
    """Load the full network rules config (mode + rules).

    Tolerant of legacy files that only contain ``{"rules": [...]}`` — the
    ``allow_all_egress`` flag defaults to False.
    """
    path = _rules_path(root)
    if not path.exists():
        return NetworkRulesConfig()
    data = json.loads(path.read_text())
    rules = [NetworkRule(**r) for r in data.get("rules", [])]
    return NetworkRulesConfig(
        allow_all_egress=bool(data.get("allow_all_egress", False)),
        rules=rules,
    )


def save_network_config(root: Path, cfg: NetworkRulesConfig):
    """Persist the full network rules config."""
    path = _rules_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "allow_all_egress": cfg.allow_all_egress,
        "rules": [asdict(r) for r in cfg.rules],
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


def load_network_rules(root: Path) -> list[NetworkRule]:
    """Back-compat shim: return just the rules list."""
    return load_network_config(root).rules


def save_network_rules(root: Path, rules: list[NetworkRule]):
    """Back-compat shim: preserve ``allow_all_egress`` while updating rules."""
    cfg = load_network_config(root)
    cfg.rules = rules
    save_network_config(root, cfg)


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

# Non-channel service hosts (always required)
PROVIDER_HOSTS: dict[str, list[NetworkRule]] = {
    "cortex": [
        NetworkRule("*.snowflakecomputing.com", 443, "Snowflake Cortex & APIs"),
    ],
    "dev_infra": [
        # Package registries
        NetworkRule("pypi.org", 443, "Python Package Index"),
        NetworkRule("files.pythonhosted.org", 443, "Python package downloads"),
        NetworkRule("registry.npmjs.org", 443, "npm package registry"),
        # Container registries
        NetworkRule("ghcr.io", 443, "GitHub Container Registry"),
        NetworkRule("*.docker.io", 443, "Docker Hub"),
        NetworkRule("production.cloudflare.docker.com", 443, "Docker Hub CDN"),
        # OpenClaw infrastructure
        NetworkRule("*.githubusercontent.com", 443, "GitHub raw content & releases"),
    ],
}

# Single source of truth for channel hosts, credentials, and config templates.
CHANNEL_REGISTRY: dict[str, dict] = {
    "telegram": {
        "display_name": "Telegram",
        "hosts": [
            NetworkRule("api.telegram.org", 443, "Telegram Bot API"),
        ],
        "credentials": [
            {"key": "TELEGRAM_BOT_TOKEN", "env_var": "TELEGRAM_BOT_TOKEN", "label": "Bot token", "prompt": "Telegram bot token (from @BotFather):", "secret": True},
            {"key": "TELEGRAM_USER_ID", "env_var": "TELEGRAM_USER_ID", "label": "Telegram user ID", "prompt": "Your Telegram user ID:", "secret": False, "inline": True, "hint": "Search for @userinfobot on Telegram and tap Start to get your numeric user ID."},
        ],
    },
    "discord": {
        "display_name": "Discord",
        "hosts": [
            NetworkRule("gateway.discord.gg", 443, "Discord Gateway WebSocket"),
            NetworkRule("discord.com", 443, "Discord REST API"),
            NetworkRule("cdn.discordapp.com", 443, "Discord CDN"),
        ],
        "credentials": [
            {"key": "DISCORD_BOT_TOKEN", "env_var": "DISCORD_BOT_TOKEN", "label": "Bot token", "prompt": "Discord bot token:", "secret": True},
            {"key": "DISCORD_USER_ID", "env_var": "DISCORD_USER_ID", "label": "Your Discord user ID", "prompt": "Your Discord user ID (enable Developer Mode, right-click avatar → Copy User ID):", "secret": False},
            {"key": "DISCORD_SERVER_ID", "env_var": "DISCORD_SERVER_ID", "label": "Discord server ID", "prompt": "Discord server/guild ID (right-click server → Copy Server ID):", "secret": False},
        ],
    },
    "slack": {
        "display_name": "Slack",
        "hosts": [
            NetworkRule("api.slack.com", 443, "Slack Web API"),
            NetworkRule("wss-primary.slack.com", 443, "Slack WebSocket (primary)"),
            NetworkRule("wss-backup.slack.com", 443, "Slack WebSocket (backup)"),
        ],
        "credentials": [
            {"key": "SLACK_BOT_TOKEN", "env_var": "SLACK_BOT_TOKEN", "label": "Bot token (xoxb-...)", "prompt": "Slack bot token (xoxb-...):", "secret": True},
            {"key": "SLACK_APP_TOKEN", "env_var": "SLACK_APP_TOKEN", "label": "App token (xapp-...)", "prompt": "Slack app token (xapp-...):", "secret": True},
        ],
    },
}


def get_channel_secrets(prefix: str, channels: list[str]) -> list[dict]:
    """Get Snowflake secret mappings for enabled channels.

    Returns list of dicts with keys: secret_name, env_var.
    Only includes credentials marked secret=True in the registry.
    """
    result = []
    for ch in channels:
        info = CHANNEL_REGISTRY.get(ch)
        if not info:
            continue
        for cred in info["credentials"]:
            if cred["secret"]:
                result.append({
                    "secret_name": f"{prefix}_{cred['env_var'].lower()}",
                    "env_var": cred["env_var"],
                })
    return result


def get_env_secrets(prefix: str, env_path: Path) -> list[dict]:
    """Get Snowflake secret mappings for ALL env vars in .env.

    Reads every key=value pair from the .env file, excluding:
    - Known config vars (not secrets)
    - Hardcoded secrets already handled by callers (SNOWFLAKE_TOKEN, GH_TOKEN, BRAVE_API_KEY)
    - Channel credential env vars (handled by get_channel_secrets())

    Returns list of dicts with keys: secret_name, env_var.
    Same format as get_channel_secrets().
    """
    # Config vars that should not become Snowflake secrets
    _EXCLUDED_CONFIG_VARS = {
        "SNOWCLAW_DB",
        "SNOWCLAW_SCHEMA",
        "SNOWCLAW_MASK_VARS",
        "SNOWFLAKE_ACCOUNT",  # connection metadata, not a secret
        "SNOWFLAKE_USER",     # connection metadata, not a secret
        "SNOWFLAKE_RUNTIME_TOKEN",  # held as sf_token Snowflake secret, not its own
        "CORTEX_BASE_URL",
        "IMAGE_TAG",
        "PROXY_LOG_RESPONSES",
    }

    # Hardcoded secrets handled explicitly by callers
    _HARDCODED_SECRETS = {
        "SNOWFLAKE_TOKEN",
        "GH_TOKEN",
        "BRAVE_API_KEY",
    }

    # Channel credential env vars (handled by get_channel_secrets())
    _channel_env_vars: set[str] = set()
    for entry in CHANNEL_REGISTRY.values():
        for cred in entry.get("credentials", []):
            _channel_env_vars.add(cred["env_var"])

    skip = _EXCLUDED_CONFIG_VARS | _HARDCODED_SECRETS | _channel_env_vars

    result = []
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in skip:
            continue
        if value.strip():
            result.append({
                "secret_name": f"{prefix}_{key.lower()}",
                "env_var": key,
            })
    return result


# ---------------------------------------------------------------------------
# Tool registry — curated developer tools that need credentials + network rules
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, dict] = {
    "github": {
        "display_name": "GitHub (gh CLI + git auth)",
        "hosts": [
            NetworkRule("github.com", 443, "GitHub"),
            NetworkRule("api.github.com", 443, "GitHub API"),
        ],
        "credentials": [
            {
                "key": "GH_TOKEN",
                "env_var": "GH_TOKEN",
                "label": "GitHub personal access token",
                "prompt": "GitHub personal access token (PAT):",
                "secret": True,
            },
        ],
        "default": True,
    },
    "brave_search": {
        "display_name": "Brave Search (web search for agent)",
        "hosts": [
            NetworkRule("api.brave.com", 443, "Brave Search API"),
        ],
        "credentials": [
            {
                "key": "BRAVE_API_KEY",
                "env_var": "BRAVE_API_KEY",
                "label": "Brave Search API key",
                "prompt": "Brave Search API key (brave.com/search/api):",
                "secret": True,
            },
        ],
        "default": False,
    },
}


def detect_required_rules(root: Path) -> list[NetworkRule]:
    """Scan project config to detect which network rules are required.

    Reads openclaw.json to find provider baseUrls and enabled channels.
    """
    rules: list[NetworkRule] = []

    # Always need Snowflake access
    rules.extend(PROVIDER_HOSTS["cortex"])

    # Always need dev infrastructure (package registries, container registries, etc.)
    rules.extend(PROVIDER_HOSTS["dev_infra"])

    config_path = root / "openclaw.json"
    if not config_path.exists():
        return _dedup(rules)

    config = json.loads(config_path.read_text())

    # Scan provider baseUrls
    providers = config.get("models", {}).get("providers", {})
    for name, provider in providers.items():
        base_url = provider.get("baseUrl", "")
        if not base_url:
            continue
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port or 443
        if host and not host.endswith(".snowflakecomputing.com") and host not in ("localhost", "127.0.0.1", "::1"):
            rules.append(NetworkRule(host, port, f"{name} provider"))

    # Scan channels — add hosts for each enabled channel from the registry
    channels = config.get("channels", {})
    for ch_key, ch_config in channels.items():
        if not ch_config.get("enabled", False):
            continue
        registry_entry = CHANNEL_REGISTRY.get(ch_key)
        if registry_entry:
            rules.extend(registry_entry["hosts"])

    # Include hosts for enabled tools (read from marker)
    marker_path = root / ".snowclaw" / "config.json"
    enabled_tools: list[str] = []
    if marker_path.exists():
        marker = json.loads(marker_path.read_text())
        enabled_tools = marker.get("tools", [])

    for tool_name in enabled_tools:
        tool = TOOL_REGISTRY.get(tool_name)
        if tool:
            rules.extend(tool["hosts"])

    return _dedup(rules)


def _dedup(rules: list[NetworkRule]) -> list[NetworkRule]:
    """Deduplicate rules by (host, port), preserving order."""
    seen: set[tuple[str, int]] = set()
    result: list[NetworkRule] = []
    for r in rules:
        key = (r.host, r.port)
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def diff_rules(
    current: list[NetworkRule], required: list[NetworkRule]
) -> tuple[list[NetworkRule], list[NetworkRule]]:
    """Compare current rules against required rules.

    Returns (added, removed) where:
    - added: rules in required but not in current
    - removed: rules in current but not in required
    """
    current_set = {(r.host, r.port) for r in current}
    required_set = {(r.host, r.port) for r in required}

    added = [r for r in required if (r.host, r.port) not in current_set]
    removed = [r for r in current if (r.host, r.port) not in required_set]
    return added, removed


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def format_rules_table(
    rules: list[NetworkRule], title: str = "Network Rules"
) -> Table:
    """Create a Rich table displaying network rules."""
    table = Table(title=title, show_lines=False, expand=False)
    table.add_column("Host", style="cyan")
    table.add_column("Port", style="dim", justify="right")
    table.add_column("Reason", style="dim")
    for r in rules:
        table.add_row(r.host, str(r.port), r.reason)
    return table


def print_diff(added: list[NetworkRule], removed: list[NetworkRule]):
    """Print a diff of rule changes."""
    for r in added:
        console.print(
            f"  [green]+[/green] [cyan]{r.host_port}[/cyan]  [dim]{r.reason}[/dim]"
        )
    for r in removed:
        console.print(
            f"  [red]-[/red] [cyan]{r.host_port}[/cyan]  [dim]{r.reason}[/dim]"
        )


def print_allow_all_warning():
    """Print the red warning shown before enabling allow-all egress mode."""
    from rich.panel import Panel

    body = (
        "[bold]Enabling allow-all egress removes SPCS's default outbound hardening.[/bold]\n\n"
        "While this mode is active the agent can reach any host on ports 443 and 80,\n"
        "including internal corporate URLs, arbitrary third-party APIs, and\n"
        "unreviewed destinations. The Cortex proxy's secret masking still runs, but\n"
        "masking is not a substitute for an egress allowlist — a compromised or\n"
        "unintentionally malicious plugin / tool can exfiltrate unmasked data.\n\n"
        "Recommended only for: internal development, exploration, and deployments\n"
        "where you have no compliance obligations. Do not enable in production for\n"
        "regulated workloads."
    )
    console.print(
        Panel(
            body,
            title="[bold red]⚠  Allow-all egress[/bold red]",
            border_style="red",
            expand=False,
        )
    )


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------


def _format_value_list(items: list[str] | tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in items)


def build_network_rule_sql(
    names: dict,
    rules: list[NetworkRule],
    allow_all: bool = False,
) -> list[str]:
    """Build standalone SQL statements for the reference ``network-rules.sql`` file.

    Uses CREATE OR REPLACE so the file can be applied to a fresh schema in one
    shot. The runtime apply path (``apply_network_rules``) prefers ALTER for
    steady-state updates so the NR object identity is preserved and the EAI
    binding stays valid without an SPCS service restart.

    When ``allow_all`` is True, the rule's VALUE_LIST is
    ``('0.0.0.0:443', '0.0.0.0:80')`` — Snowflake's documented allow-all
    pattern — and the ``rules`` argument is ignored.
    """
    if allow_all:
        value_list = _format_value_list(ALLOW_ALL_VALUE_LIST)
    elif rules:
        value_list = _format_value_list([r.host_port for r in rules])
    else:
        return []

    s = names["schema"]
    return [
        (
            f"CREATE OR REPLACE NETWORK RULE {s}.{names['egress_rule']} "
            f"MODE = EGRESS TYPE = HOST_PORT VALUE_LIST = ({value_list})"
        ),
        (
            f"CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION {names['external_access']} "
            f"ALLOWED_NETWORK_RULES = ({s}.{names['egress_rule']}) "
            "ENABLED = TRUE"
        ),
    ]


# ---------------------------------------------------------------------------
# Apply to Snowflake
# ---------------------------------------------------------------------------


def _network_rule_exists(
    account: str, pat: str, schema_fqn: str, name: str, role: str | None = None
) -> bool:
    """Return True if a network rule with ``name`` exists in ``schema_fqn`` (db.schema)."""
    sql = f"SHOW NETWORK RULES LIKE '{name}' IN SCHEMA {schema_fqn}"
    result = snowflake_rest_execute(account, pat, sql, role=role)
    return bool(result.get("data"))


def _external_access_integration_exists(
    account: str, pat: str, name: str, role: str | None = None
) -> bool:
    """Return True if an external access integration with ``name`` exists."""
    sql = f"SHOW EXTERNAL ACCESS INTEGRATIONS LIKE '{name}'"
    result = snowflake_rest_execute(account, pat, sql, role=role)
    return bool(result.get("data"))


def apply_network_rules(
    account: str,
    pat: str,
    names: dict,
    rules: list[NetworkRule],
    allow_all: bool = False,
    admin_role: str | None = None,
) -> bool:
    """Apply network rules to Snowflake via REST API.

    Steady-state updates use ``ALTER NETWORK RULE ... SET VALUE_LIST`` so the
    NR object identity is preserved — the EAI binding stays valid and the SPCS
    service picks up the new host list without a restart. The NR and EAI are
    only issued as ``CREATE`` when they don't already exist.

    When ``allow_all`` is True, the applied VALUE_LIST is
    ``('0.0.0.0:443', '0.0.0.0:80')`` regardless of ``rules``.

    Returns True if successful, False otherwise.
    """
    if allow_all:
        value_list = _format_value_list(ALLOW_ALL_VALUE_LIST)
    elif rules:
        value_list = _format_value_list([r.host_port for r in rules])
    else:
        console.print("  [dim]No network rules to apply.[/dim]")
        return True

    s = names["schema"]
    egress = names["egress_rule"]
    eai = names["external_access"]

    try:
        nr_exists = _network_rule_exists(account, pat, s, egress, role=admin_role)
        eai_exists = _external_access_integration_exists(account, pat, eai, role=admin_role)
    except requests.HTTPError as e:
        console.print("  [red]✗[/red] Failed to query existing network objects")
        console.print(f"    [dim]{e}[/dim]")
        return False

    statements: list[tuple[str, str]] = []  # (label, sql)
    if nr_exists:
        statements.append((
            f"ALTER NETWORK RULE {s}.{egress}",
            f"ALTER NETWORK RULE {s}.{egress} SET VALUE_LIST = ({value_list})",
        ))
    else:
        statements.append((
            f"CREATE NETWORK RULE {s}.{egress}",
            (
                f"CREATE NETWORK RULE {s}.{egress} "
                f"MODE = EGRESS TYPE = HOST_PORT VALUE_LIST = ({value_list})"
            ),
        ))
    if not eai_exists:
        statements.append((
            f"CREATE EXTERNAL ACCESS INTEGRATION {eai}",
            (
                f"CREATE EXTERNAL ACCESS INTEGRATION {eai} "
                f"ALLOWED_NETWORK_RULES = ({s}.{egress}) ENABLED = TRUE"
            ),
        ))

    with console.status("[bold cyan]Applying network rules..."):
        for label, stmt in statements:
            try:
                snowflake_rest_execute(account, pat, stmt, role=admin_role)
                console.print(f"  [green]✓[/green] {label}")
            except requests.HTTPError as e:
                console.print(f"  [red]✗[/red] Failed: {label}")
                console.print(f"    [dim]{e}[/dim]")
                return False
    return True


# ---------------------------------------------------------------------------
# Interactive approval flow
# ---------------------------------------------------------------------------


def prompt_and_apply_rules(
    root: Path,
    account: str,
    pat: str,
    names: dict,
    detected: list[NetworkRule] | None = None,
    admin_role: str | None = None,
) -> list[NetworkRule]:
    """Detect required rules, show diff, prompt for approval, and apply.

    Returns the final approved rule list. In allow-all mode this short-circuits
    the diff flow and just ensures the NR is in allow-all state.
    """
    from InquirerPy import inquirer

    cfg = load_network_config(root)
    current = cfg.rules
    if cfg.allow_all_egress:
        console.print(
            "[bold red]Egress mode: ALLOW ALL[/bold red] "
            "[dim](unrestricted — 0.0.0.0:443, 0.0.0.0:80)[/dim]"
        )
        apply_network_rules(account, pat, names, current, allow_all=True, admin_role=admin_role)
        return current

    if detected is None:
        detected = detect_required_rules(root)

    # First-time setup: no existing rules
    if not current:
        if not detected:
            console.print("[dim]No network rules required.[/dim]")
            return []

        console.print()
        console.print("[bold]The following network rules are required for external access:[/bold]")
        for r in detected:
            console.print(
                f"  [green]+[/green] [cyan]{r.host_port}[/cyan]  [dim]{r.reason}[/dim]"
            )

        console.print()
        approved = inquirer.confirm(
            message="Approve these network rules?",
            default=True,
        ).execute()

        if not approved:
            console.print(
                "[yellow]Network rules not approved. External access will be unavailable.[/yellow]"
            )
            return []

        save_network_rules(root, detected)
        apply_network_rules(account, pat, names, detected, admin_role=admin_role)
        return detected

    # Existing rules — check for changes
    added, removed = diff_rules(current, detected)

    if not added and not removed:
        console.print("[dim]Network rules are up to date.[/dim]")
        return current

    console.print()
    console.print("[bold]Network rule changes detected:[/bold]")
    print_diff(added, removed)

    console.print()
    approved = inquirer.confirm(
        message="Approve these changes?",
        default=True,
    ).execute()

    if not approved:
        console.print("[dim]Keeping existing network rules.[/dim]")
        return current

    # Merge: apply additions and removals
    removed_set = {(r.host, r.port) for r in removed}
    merged = [r for r in current if (r.host, r.port) not in removed_set]
    existing_set = {(r.host, r.port) for r in merged}
    for r in added:
        if (r.host, r.port) not in existing_set:
            merged.append(r)

    save_network_rules(root, merged)
    apply_network_rules(account, pat, names, merged, admin_role=admin_role)
    return merged


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_host_port(value: str) -> tuple[str, int]:
    """Parse 'host:port' or 'host' into (host, port). Defaults port to 443."""
    if ":" in value:
        host, port_str = value.rsplit(":", 1)
        try:
            return host, int(port_str)
        except ValueError:
            pass
    return value, 443


def offer_apply_rules(root: Path):
    """Ask whether to apply rules to Snowflake now."""
    from InquirerPy import inquirer

    apply_now = inquirer.confirm(
        message="Apply to Snowflake now?",
        default=False,
    ).execute()

    if apply_now:
        ctx = load_snowflake_context(root)
        if not ctx["account"] or not ctx["token"]:
            console.print("[red]Missing Snowflake credentials in .env.[/red]")
            return
        cfg = load_network_config(root)
        admin_role = ctx["marker"].get("admin_role") or ctx["conn"].get("role")
        success = apply_network_rules(
            ctx["account"], ctx["token"], ctx["names"], cfg.rules,
            allow_all=cfg.allow_all_egress,
            admin_role=admin_role,
        )
        if success:
            console.print("[green]Network rules applied to Snowflake.[/green]")
        else:
            console.print("[red]Failed to apply. Retry with [cyan]snowclaw network apply[/cyan].[/red]")

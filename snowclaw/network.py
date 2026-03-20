"""Network rule management for SPCS external access."""

from __future__ import annotations

import json
import re
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


def _rules_path(root: Path) -> Path:
    return root / ".snowclaw" / RULES_FILE


def load_network_rules(root: Path) -> list[NetworkRule]:
    """Load network rules from .snowclaw/network-rules.json."""
    path = _rules_path(root)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [NetworkRule(**r) for r in data.get("rules", [])]


def save_network_rules(root: Path, rules: list[NetworkRule]):
    """Save network rules to .snowclaw/network-rules.json."""
    path = _rules_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"rules": [asdict(r) for r in rules]}
    path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

# Non-channel service hosts (always required)
PROVIDER_HOSTS: dict[str, list[NetworkRule]] = {
    "cortex": [
        NetworkRule("*.snowflakecomputing.com", 443, "Snowflake Cortex & APIs"),
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
        if host and not host.endswith(".snowflakecomputing.com"):
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


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------


def build_network_rule_sql(names: dict, rules: list[NetworkRule]) -> list[str]:
    """Build SQL statements to create/replace network rule and external access integration."""
    if not rules:
        return []

    s = names["schema"]
    value_list = ", ".join(f"'{r.host_port}'" for r in rules)

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


def apply_network_rules(
    account: str, pat: str, names: dict, rules: list[NetworkRule]
) -> bool:
    """Apply network rules to Snowflake via REST API.

    Returns True if successful, False otherwise.
    """
    stmts = build_network_rule_sql(names, rules)
    if not stmts:
        console.print("  [dim]No network rules to apply.[/dim]")
        return True

    _LABEL_RE = re.compile(
        r"(?:CREATE|REPLACE)\s+(?:OR\s+REPLACE\s+)?(\w[\w\s]+?)\s+\S+\.\S+",
        re.IGNORECASE,
    )

    with console.status("[bold cyan]Applying network rules..."):
        for stmt in stmts:
            try:
                snowflake_rest_execute(account, pat, stmt)
                m = _LABEL_RE.search(stmt)
                label = m.group(0).split("REPLACE ")[-1] if m else stmt[:60]
                console.print(f"  [green]✓[/green] {label}")
            except requests.HTTPError as e:
                console.print(f"  [red]✗[/red] Failed: {stmt[:80]}...")
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
) -> list[NetworkRule]:
    """Detect required rules, show diff, prompt for approval, and apply.

    Returns the final approved rule list.
    """
    from InquirerPy import inquirer

    current = load_network_rules(root)
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
        apply_network_rules(account, pat, names, detected)
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
    apply_network_rules(account, pat, names, merged)
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
        rules = load_network_rules(root)
        success = apply_network_rules(ctx["account"], ctx["token"], ctx["names"], rules)
        if success:
            console.print("[green]Network rules applied to Snowflake.[/green]")
        else:
            console.print("[red]Failed to apply. Retry with [cyan]snowclaw network apply[/cyan].[/red]")

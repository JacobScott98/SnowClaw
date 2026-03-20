"""Channel configuration management for communication integrations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from InquirerPy import inquirer
from rich.panel import Panel
from rich.table import Table

from snowclaw.network import (
    NetworkRule,
    load_network_rules,
    offer_apply_rules,
    save_network_rules,
)
from snowclaw.utils import console, find_project_root, load_dotenv, render_banner


# ---------------------------------------------------------------------------
# Channel type definitions
# ---------------------------------------------------------------------------


@dataclass
class ChannelField:
    """A single credential/config field for a channel type."""

    name: str
    env_var_template: str  # e.g. "SLACK_BOT_TOKEN" or "{PREFIX}_BOT_TOKEN"
    config_key: str  # key in openclaw.json accounts dict, e.g. "botToken"
    prompt: str  # prompt text for interactive input
    secret: bool = True  # whether to mask input


@dataclass
class ChannelType:
    """Definition of a supported channel type."""

    name: str  # e.g. "slack", "telegram", "discord"
    display_name: str
    fields: list[ChannelField]
    extra_config: dict = field(default_factory=dict)  # e.g. {"mode": "socket"}
    network_rules: list[NetworkRule] = field(default_factory=list)


CHANNEL_TYPES: dict[str, ChannelType] = {
    "slack": ChannelType(
        name="slack",
        display_name="Slack",
        fields=[
            ChannelField(
                name="bot_token",
                env_var_template="SLACK_BOT_TOKEN",
                config_key="botToken",
                prompt="Slack bot token (xoxb-...):",
            ),
            ChannelField(
                name="app_token",
                env_var_template="SLACK_APP_TOKEN",
                config_key="appToken",
                prompt="Slack app token (xapp-...):",
            ),
        ],
        extra_config={"mode": "socket"},
        network_rules=[
            NetworkRule("api.slack.com", 443, "Slack Web API"),
            NetworkRule("wss-primary.slack.com", 443, "Slack WebSocket (primary)"),
            NetworkRule("wss-backup.slack.com", 443, "Slack WebSocket (backup)"),
        ],
    ),
    "telegram": ChannelType(
        name="telegram",
        display_name="Telegram",
        fields=[
            ChannelField(
                name="bot_token",
                env_var_template="TELEGRAM_BOT_TOKEN",
                config_key="botToken",
                prompt="Telegram bot token (from @BotFather):",
            ),
        ],
        network_rules=[
            NetworkRule("api.telegram.org", 443, "Telegram Bot API"),
        ],
    ),
    "discord": ChannelType(
        name="discord",
        display_name="Discord",
        fields=[
            ChannelField(
                name="bot_token",
                env_var_template="DISCORD_BOT_TOKEN",
                config_key="botToken",
                prompt="Discord bot token:",
            ),
        ],
        network_rules=[
            NetworkRule("discord.com", 443, "Discord API"),
            NetworkRule("gateway.discord.gg", 443, "Discord Gateway WebSocket"),
        ],
    ),
}


# ---------------------------------------------------------------------------
# openclaw.json channel operations
# ---------------------------------------------------------------------------


def load_openclaw_config(root: Path) -> dict:
    """Load and return the full openclaw.json config."""
    config_path = root / "openclaw.json"
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text())


def save_openclaw_config(root: Path, config: dict):
    """Write the full openclaw.json config."""
    config_path = root / "openclaw.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def get_configured_channels(root: Path) -> dict[str, dict]:
    """Return the channels dict from openclaw.json."""
    config = load_openclaw_config(root)
    return config.get("channels", {})


def add_channel_to_config(
    root: Path,
    channel_type: str,
    account_name: str,
    credentials: dict[str, str],
    extra_config: dict | None = None,
):
    """Add a channel entry to openclaw.json.

    credentials: mapping of config_key -> env var placeholder, e.g. {"botToken": "${SLACK_BOT_TOKEN}"}
    """
    config = load_openclaw_config(root)
    if "channels" not in config:
        config["channels"] = {}

    channel_entry: dict = {"enabled": True}
    if extra_config:
        channel_entry.update(extra_config)

    # Merge with existing accounts if channel type already has entries
    existing = config["channels"].get(channel_type, {})
    existing_accounts = existing.get("accounts", {})
    existing_accounts[account_name] = credentials
    channel_entry["accounts"] = existing_accounts

    config["channels"][channel_type] = channel_entry
    save_openclaw_config(root, config)


def remove_channel_from_config(root: Path, channel_type: str) -> bool:
    """Remove a channel type entirely from openclaw.json. Returns True if found."""
    config = load_openclaw_config(root)
    channels = config.get("channels", {})
    if channel_type not in channels:
        return False
    del channels[channel_type]
    config["channels"] = channels
    save_openclaw_config(root, config)
    return True


def update_channel_credentials(
    root: Path,
    channel_type: str,
    account_name: str,
    credentials: dict[str, str],
):
    """Update credentials for an existing channel account."""
    config = load_openclaw_config(root)
    channels = config.get("channels", {})
    if channel_type not in channels:
        return False
    accounts = channels[channel_type].get("accounts", {})
    if account_name not in accounts:
        return False
    accounts[account_name].update(credentials)
    save_openclaw_config(root, config)
    return True


# ---------------------------------------------------------------------------
# .env operations
# ---------------------------------------------------------------------------


def add_env_vars(root: Path, new_vars: dict[str, str]):
    """Append environment variables to .env, skipping any that already exist."""
    env_path = root / ".env"
    existing = load_dotenv(env_path)

    lines_to_add = []
    for key, value in new_vars.items():
        if key not in existing:
            lines_to_add.append(f"{key}={value}")

    if not lines_to_add:
        return

    # Read existing content to append
    content = ""
    if env_path.exists():
        content = env_path.read_text()
        if not content.endswith("\n"):
            content += "\n"

    content += "\n".join(lines_to_add) + "\n"
    env_path.write_text(content)


def remove_env_vars(root: Path, keys: list[str]):
    """Remove environment variables from .env by key."""
    env_path = root / ".env"
    if not env_path.exists():
        return

    lines = env_path.read_text().splitlines()
    keys_set = set(keys)
    filtered = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in keys_set:
                continue
        filtered.append(line)
    env_path.write_text("\n".join(filtered) + "\n")


def get_env_var_keys_for_channel(channel_type: str) -> list[str]:
    """Return the .env variable names used by a channel type."""
    ct = CHANNEL_TYPES.get(channel_type)
    if not ct:
        return []
    return [f.env_var_template for f in ct.fields]


# ---------------------------------------------------------------------------
# Network rule helpers
# ---------------------------------------------------------------------------


def get_channel_network_rules(channel_type: str) -> list[NetworkRule]:
    """Return the network rules required by a channel type."""
    ct = CHANNEL_TYPES.get(channel_type)
    if not ct:
        return []
    return list(ct.network_rules)


def mask_value(value: str, show: int = 4) -> str:
    """Mask a secret value, showing only the last `show` characters."""
    if len(value) <= show:
        return "*" * len(value)
    return "*" * (len(value) - show) + value[-show:]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def format_channels_table(channels: dict[str, dict]) -> Table:
    """Create a Rich table displaying configured channels."""
    table = Table(title="Configured Channels", show_lines=False, expand=False)
    table.add_column("Type", style="cyan")
    table.add_column("Account", style="white")
    table.add_column("Enabled", style="green")
    table.add_column("Details", style="dim")

    for ch_type, ch_config in channels.items():
        enabled = "yes" if ch_config.get("enabled", False) else "no"
        accounts = ch_config.get("accounts", {})
        mode = ch_config.get("mode", "")
        details = f"mode={mode}" if mode else ""

        if not accounts:
            table.add_row(ch_type, "-", enabled, details)
        else:
            for acct_name in accounts:
                table.add_row(ch_type, acct_name, enabled, details)

    return table


# ---------------------------------------------------------------------------
# Interactive flows
# ---------------------------------------------------------------------------


def channel_list():
    """List configured channels."""
    render_banner()
    root = find_project_root()
    channels = get_configured_channels(root)

    if not channels:
        console.print("[dim]No channels configured.[/dim]")
        console.print(
            "Run [cyan]snowclaw channel add[/cyan] to add a communication channel."
        )
        return

    console.print(format_channels_table(channels))
    console.print(f"\n[dim]{len(channels)} channel(s) configured[/dim]")


def channel_add():
    """Interactive wizard to add a channel."""
    render_banner()
    root = find_project_root()
    channels = get_configured_channels(root)

    # Step 1: Choose channel type
    choices = [
        {"name": ct.display_name, "value": ct.name}
        for ct in CHANNEL_TYPES.values()
    ]
    channel_type = inquirer.select(
        message="Channel type:",
        choices=choices,
    ).execute()

    ct = CHANNEL_TYPES[channel_type]

    # Check if already configured
    if channel_type in channels:
        existing_accounts = list(channels[channel_type].get("accounts", {}).keys())
        if existing_accounts:
            console.print(
                f"[yellow]{ct.display_name} already has account(s): "
                f"{', '.join(existing_accounts)}[/yellow]"
            )
            add_another = inquirer.confirm(
                message="Add another account?",
                default=False,
            ).execute()
            if not add_another:
                console.print("[dim]Aborted.[/dim]")
                return

    # Step 2: Account name
    account_name = inquirer.text(
        message="Account name:",
        default="default",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="Account name is required.",
    ).execute().strip()

    # Check for duplicate account name
    if channel_type in channels:
        existing_accounts = channels[channel_type].get("accounts", {})
        if account_name in existing_accounts:
            console.print(
                f"[red]Account '{account_name}' already exists for {ct.display_name}.[/red]"
            )
            console.print(
                f"Use [cyan]snowclaw channel edit {channel_type}[/cyan] to modify it."
            )
            return

    # Step 3: Collect credentials
    console.print()
    console.print(f"[bold]Configure {ct.display_name} credentials:[/bold]")
    env_vars: dict[str, str] = {}
    config_credentials: dict[str, str] = {}

    for fld in ct.fields:
        if fld.secret:
            value = inquirer.secret(
                message=fld.prompt,
                validate=lambda v: len(v.strip()) > 0,
                invalid_message=f"{fld.name} is required.",
            ).execute().strip()
        else:
            value = inquirer.text(
                message=fld.prompt,
                validate=lambda v: len(v.strip()) > 0,
                invalid_message=f"{fld.name} is required.",
            ).execute().strip()

        env_vars[fld.env_var_template] = value
        config_credentials[fld.config_key] = f"${{{fld.env_var_template}}}"

    # Step 4: Save to openclaw.json and .env
    add_env_vars(root, env_vars)
    add_channel_to_config(
        root, channel_type, account_name, config_credentials, ct.extra_config or None
    )
    console.print()
    console.print(f"[green]✓[/green] Added {ct.display_name} channel (account: {account_name})")
    console.print(f"  [dim]Updated openclaw.json and .env[/dim]")

    # Step 5: Network rules
    rules_needed = get_channel_network_rules(channel_type)
    if rules_needed:
        current_rules = load_network_rules(root)
        current_set = {(r.host, r.port) for r in current_rules}
        new_rules = [r for r in rules_needed if (r.host, r.port) not in current_set]

        if new_rules:
            console.print()
            console.print(
                f"[bold]{ct.display_name} requires the following network rules:[/bold]"
            )
            for r in new_rules:
                console.print(
                    f"  [green]+[/green] [cyan]{r.host_port}[/cyan]  [dim]{r.reason}[/dim]"
                )
            console.print()
            approve = inquirer.confirm(
                message="Add these network rules?",
                default=True,
            ).execute()
            if approve:
                merged = current_rules + new_rules
                save_network_rules(root, merged)
                console.print("[green]✓[/green] Network rules saved.")
                offer_apply_rules(root)
            else:
                console.print(
                    "[dim]Network rules not added. Add them later with "
                    "[cyan]snowclaw network add <host>[/cyan].[/dim]"
                )
        else:
            console.print()
            console.print("[dim]Required network rules already configured.[/dim]")

    # Step 6: Summary
    console.print()
    console.print(Panel(
        f"[bold]{ct.display_name} channel configured![/bold]\n\n"
        f"  Account:  [cyan]{account_name}[/cyan]\n"
        + "".join(
            f"  {fld.config_key}:  [dim]${{{fld.env_var_template}}}[/dim]\n"
            for fld in ct.fields
        )
        + "\nRun [cyan]snowclaw dev[/cyan] to test locally.",
        title="Channel Added",
        border_style="green",
        expand=False,
    ))


def channel_remove(name: str | None):
    """Remove a channel configuration."""
    render_banner()
    root = find_project_root()

    if not name:
        console.print("[red]Usage: snowclaw channel remove <channel-type>[/red]")
        return

    channels = get_configured_channels(root)
    if name not in channels:
        console.print(f"[yellow]No channel '{name}' found.[/yellow]")
        if channels:
            console.print(
                f"Configured channels: {', '.join(channels.keys())}"
            )
        return

    ct = CHANNEL_TYPES.get(name)
    display = ct.display_name if ct else name

    confirm = inquirer.confirm(
        message=f"Remove {display} channel and its credentials from .env?",
        default=False,
    ).execute()
    if not confirm:
        console.print("[dim]Aborted.[/dim]")
        return

    # Remove env vars
    env_keys = get_env_var_keys_for_channel(name)
    if env_keys:
        remove_env_vars(root, env_keys)

    # Remove from openclaw.json
    remove_channel_from_config(root, name)
    console.print(f"[green]✓[/green] Removed {display} channel.")
    console.print(
        "[dim]Network rules were not removed. Use [cyan]snowclaw network detect[/cyan] "
        "to clean up unused rules.[/dim]"
    )


def channel_edit(name: str | None):
    """Edit an existing channel configuration."""
    render_banner()
    root = find_project_root()

    if not name:
        console.print("[red]Usage: snowclaw channel edit <channel-type>[/red]")
        return

    channels = get_configured_channels(root)
    if name not in channels:
        console.print(f"[yellow]No channel '{name}' found.[/yellow]")
        if channels:
            console.print(
                f"Configured channels: {', '.join(channels.keys())}"
            )
        return

    ct = CHANNEL_TYPES.get(name)
    if not ct:
        console.print(f"[red]Unknown channel type '{name}'.[/red]")
        return

    ch_config = channels[name]
    accounts = ch_config.get("accounts", {})

    # Pick account to edit
    if len(accounts) == 1:
        account_name = next(iter(accounts))
    elif len(accounts) > 1:
        account_name = inquirer.select(
            message="Which account to edit?",
            choices=list(accounts.keys()),
        ).execute()
    else:
        console.print(f"[yellow]No accounts configured for {ct.display_name}.[/yellow]")
        return

    # Load current .env values
    env = load_dotenv(root / ".env")

    console.print(
        f"\n[bold]Editing {ct.display_name} / {account_name}[/bold]"
    )
    console.print("[dim]Press Enter to keep current value, or type a new one.[/dim]\n")

    new_env_vars: dict[str, str] = {}
    config_credentials: dict[str, str] = {}
    changed = False

    for fld in ct.fields:
        current_val = env.get(fld.env_var_template, "")
        masked = mask_value(current_val) if current_val else "(not set)"

        new_val = inquirer.secret(
            message=f"{fld.prompt} [current: {masked}]",
        ).execute().strip()

        if new_val:
            new_env_vars[fld.env_var_template] = new_val
            changed = True
        else:
            # Keep existing value
            if current_val:
                new_env_vars[fld.env_var_template] = current_val

        config_credentials[fld.config_key] = f"${{{fld.env_var_template}}}"

    if not changed:
        console.print("[dim]No changes made.[/dim]")
        return

    # Update .env (remove old, add new)
    env_keys = [fld.env_var_template for fld in ct.fields]
    remove_env_vars(root, env_keys)
    add_env_vars(root, new_env_vars)

    # Update openclaw.json credentials
    update_channel_credentials(root, name, account_name, config_credentials)

    console.print()
    console.print(f"[green]✓[/green] Updated {ct.display_name} / {account_name}")
    console.print(f"  [dim]Updated openclaw.json and .env[/dim]")

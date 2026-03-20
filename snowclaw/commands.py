"""CLI command implementations."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from InquirerPy import inquirer
from rich.panel import Panel

from snowclaw import __version__
from snowclaw.config import write_connections_toml, write_dotenv, write_openclaw_config
from snowclaw.network import (
    CHANNEL_REGISTRY,
    TOOL_REGISTRY,
    NetworkRule,
    apply_network_rules,
    detect_required_rules,
    diff_rules,
    format_rules_table,
    get_channel_secrets,
    load_network_rules,
    parse_host_port,
    print_diff,
    save_network_rules,
)
from snowclaw.scaffold import assemble_build_context, scaffold_user_files
from snowclaw.snowflake import run_snowflake_setup
from snowclaw.utils import (
    console,
    find_project_root,
    get_templates_dir,
    load_snowflake_context,
    read_marker,
    render_banner,
    sf_names,
    snowflake_rest_execute,
    write_marker,
)


def cmd_setup(args: argparse.Namespace):
    """Interactive first-time setup wizard."""
    render_banner()
    cwd = Path.cwd()
    force = getattr(args, "force", False)

    # Refuse to scaffold inside the CLI repo itself
    cli_repo = get_templates_dir().parent
    if cwd.resolve() == cli_repo.resolve():
        console.print("[red]Cannot run setup inside the snowclaw CLI repo.[/red]")
        console.print("Create a new directory and run [cyan]snowclaw setup[/cyan] there:")
        console.print("  [dim]mkdir my-openclaw && cd my-openclaw && snowclaw setup[/dim]")
        sys.exit(1)

        # Warn if directory is non-empty (ignoring .git)
    contents = [p for p in cwd.iterdir() if p.name != ".git"]
    if contents and not force:
        proceed = inquirer.confirm(
            message=f"Directory is not empty ({len(contents)} items). Scaffold here anyway?",
            default=False,
        ).execute()
        if not proceed:
            console.print("[dim]Aborted.[/dim]")
            return

    console.print("[bold]Scaffolding project files...[/bold]")
    copied, skipped = scaffold_user_files(cwd, force=force)
    for f in copied:
        console.print(f"  [green]✓[/green] {f}")
    for f in skipped:
        console.print(f"  [dim]  skipped {f} (already exists)[/dim]")
    console.print()
    root = cwd

    # --- Collect inputs ---
    account = inquirer.text(
        message="Snowflake account identifier (orgname-accountname):",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="Account identifier is required.",
    ).execute()

    sf_user = inquirer.text(
        message="Snowflake username:",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="Username is required.",
    ).execute()

    pat = inquirer.secret(
        message="Programmatic access token (PAT):",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="PAT is required.",
    ).execute()

    channels = inquirer.checkbox(
        message="Communication channels to enable:",
        choices=[
            {"name": "Telegram (easiest setup)", "value": "telegram"},
            {"name": "Discord", "value": "discord"},
            {"name": "Slack", "value": "slack"},
        ],
    ).execute()

    # Collect credentials for each selected channel
    channel_creds: dict[str, str] = {}
    for ch_key in channels:
        entry = CHANNEL_REGISTRY.get(ch_key)
        if not entry:
            continue
        console.print(f"\n[bold]{entry['display_name']} credentials:[/bold]")
        for cred in entry["credentials"]:
            if cred["secret"]:
                value = inquirer.secret(
                    message=cred["prompt"],
                    validate=lambda v: len(v.strip()) > 0,
                    invalid_message=f"{cred['label']} is required.",
                ).execute()
            else:
                value = inquirer.text(
                    message=cred["prompt"],
                    validate=lambda v: len(v.strip()) > 0,
                    invalid_message=f"{cred['label']} is required.",
                ).execute()
            channel_creds[cred["env_var"]] = value.strip()

    # --- Developer tools ---
    tools = inquirer.checkbox(
        message="Developer tools to enable:",
        choices=[
            {"name": t["display_name"], "value": name, "enabled": t.get("default", False)}
            for name, t in TOOL_REGISTRY.items()
        ],
    ).execute()

    tool_credentials: dict[str, str] = {}
    for tool_name in tools:
        tool = TOOL_REGISTRY[tool_name]
        for cred in tool["credentials"]:
            if cred.get("secret"):
                value = inquirer.secret(
                    message=cred["prompt"],
                    validate=lambda v: len(v.strip()) > 0,
                    invalid_message=f"{cred['label']} is required.",
                ).execute()
            else:
                value = inquirer.text(message=cred["prompt"]).execute()
            tool_credentials[cred["env_var"]] = value.strip()

    warehouse = inquirer.text(message="Snowflake warehouse:", default="COMPUTE_WH").execute()
    role = inquirer.text(message="Snowflake role:", default="SYSADMIN").execute()
    database = inquirer.text(
        message="Snowflake database name:",
        default="snowclaw_db",
        validate=lambda v: bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", v.strip())),
        invalid_message="Database name must be alphanumeric with underscores, starting with a letter.",
    ).execute().strip()
    schema = inquirer.text(
        message="Snowflake schema name:",
        default="snowclaw_schema",
        validate=lambda v: bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", v.strip())),
        invalid_message="Schema name must be alphanumeric with underscores, starting with a letter.",
    ).execute().strip()

    settings = {
        "account": account.strip(),
        "sf_user": sf_user.strip(),
        "pat": pat.strip(),
        "channels": channels,
        "warehouse": warehouse.strip(),
        "role": role.strip(),
        "database": database,
        "schema": schema,
        **channel_creds,
        "tools": tools,
        "tool_credentials": tool_credentials,
    }

    # --- Write .snowclaw marker ---
    marker = {
        "version": __version__,
        "created": datetime.now(timezone.utc).isoformat(),
        "database": database,
        "schema": schema,
        "openclaw_version": "latest",
        "tools": tools,
    }
    write_marker(root, marker)

    # --- Write config files ---
    console.print()
    console.print("[bold]Writing configuration files...[/bold]")
    write_dotenv(root, settings)
    write_openclaw_config(root, settings)
    write_connections_toml(root, settings)

    # --- Detect and approve network rules ---
    console.print()
    console.print("[bold]Detecting required network rules...[/bold]")
    detected = detect_required_rules(root)
    names = sf_names(settings["database"], settings["schema"])

    if detected:
        console.print()
        console.print("[bold]The following network rules are required for external access:[/bold]")
        for r in detected:
            console.print(
                f"  [green]+[/green] [cyan]{r.host_port}[/cyan]  [dim]{r.reason}[/dim]"
            )
        console.print()
        approve_rules = inquirer.confirm(
            message="Approve these network rules?",
            default=True,
        ).execute()
        if approve_rules:
            save_network_rules(root, detected)
            console.print("[green]Network rules saved.[/green]")
        else:
            console.print(
                "[yellow]Network rules not approved. External access will be unavailable.[/yellow]"
            )
            console.print(
                "[dim]You can add rules later with [cyan]snowclaw network add <host>[/cyan][/dim]"
            )
    else:
        console.print("  [dim]No external network rules detected.[/dim]")

    # --- Optionally create Snowflake objects ---
    console.print()
    create_objects = inquirer.confirm(
        message="Create Snowflake objects now? (database, schema, compute pool, etc.)",
        default=True,
    ).execute()

    approved_rules = load_network_rules(root)

    if create_objects:
        console.print()
        try:
            run_snowflake_setup(settings)
            console.print()
            console.print("[green]Snowflake objects created successfully.[/green]")
        except Exception:
            console.print("[yellow]Some objects may not have been created. You can retry or create them manually.[/yellow]")

        # Apply approved network rules
        if approved_rules:
            console.print()
            apply_network_rules(
                settings["account"], settings["pat"], names, approved_rules
            )

    # --- Summary ---
    console.print()
    console.print(Panel(
        "[bold]Setup complete![/bold]\n\n"
        "Next steps:\n"
        "  [cyan]snowclaw dev[/cyan]             — run locally\n"
        "  [cyan]snowclaw deploy[/cyan]          — deploy to SPCS\n",
        title="What's next",
        border_style="green",
        expand=False,
    ))


def cmd_dev(args: argparse.Namespace):
    """Assemble build context and run docker compose up."""
    render_banner()
    root = find_project_root()

    console.print("[bold]Assembling build context...[/bold]")
    build_dir = assemble_build_context(root)
    console.print(f"  [green]✓[/green] Build context ready at {build_dir.relative_to(root)}")
    console.print()

    compose_file = build_dir / "docker-compose.yml"
    console.print("[bold]Starting OpenClaw...[/bold]")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "--build"],
            cwd=str(root),
            check=True,
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Docker compose failed (exit {e.returncode}).[/red]")
        sys.exit(e.returncode)


def cmd_build(args: argparse.Namespace):
    """Assemble build context and run docker build."""
    render_banner()
    root = find_project_root()

    console.print("[bold]Assembling build context...[/bold]")
    build_dir = assemble_build_context(root)
    console.print(f"  [green]✓[/green] Build context ready at {build_dir.relative_to(root)}")
    console.print()

    marker = read_marker(root)
    image_tag = getattr(args, "tag", None) or "latest"

    console.print("[bold]Building Docker image...[/bold]")
    result = subprocess.run(
        ["docker", "build", "-t", f"snowclaw:{image_tag}", str(build_dir)],
    )
    if result.returncode != 0:
        console.print("[red]Build failed.[/red]")
        sys.exit(result.returncode)

    console.print()
    console.print(f"[green]✓[/green] Built image [cyan]snowclaw:{image_tag}[/cyan]")


def cmd_deploy(args: argparse.Namespace):
    """Build, push, and deploy to SPCS."""
    render_banner()
    root = find_project_root()
    ctx = load_snowflake_context(root)

    account = ctx["account"]
    token = ctx["token"]
    sf_user = ctx["user"]
    names = ctx["names"]
    env = ctx["env"]

    if not all([account, token, sf_user]):
        console.print("[red]Missing required environment variables in .env.[/red]")
        console.print("Required: SNOWFLAKE_ACCOUNT, SNOWFLAKE_TOKEN, SNOWFLAKE_USER")
        sys.exit(1)

    image_tag = env.get("IMAGE_TAG", "latest")
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    warehouse = ctx["warehouse"]
    repo = names["repo"]
    registry_host = f"{account}.registry.snowflakecomputing.com"
    image_repo = f"{registry_host}/{db}/{schema_name}/{repo}"

    # Check network rules before deploying
    console.print("[bold]Checking network rules...[/bold]")
    current_rules = load_network_rules(root)
    detected_rules = detect_required_rules(root)
    added, removed = diff_rules(current_rules, detected_rules)

    if added or removed:
        console.print()
        console.print("[bold]Network rule changes detected:[/bold]")
        print_diff(added, removed)
        console.print()
        approve = inquirer.confirm(
            message="Approve these network rule changes?",
            default=True,
        ).execute()
        if approve:
            # Merge changes
            removed_set = {(r.host, r.port) for r in removed}
            merged = [r for r in current_rules if (r.host, r.port) not in removed_set]
            existing_set = {(r.host, r.port) for r in merged}
            for r in added:
                if (r.host, r.port) not in existing_set:
                    merged.append(r)
            save_network_rules(root, merged)
            apply_network_rules(account, token, names, merged)
            console.print()
        else:
            console.print("[dim]Keeping existing network rules.[/dim]")
            if current_rules:
                console.print()
    elif current_rules:
        console.print(f"  [green]✓[/green] {len(current_rules)} rules up to date")
        console.print()
    else:
        console.print("  [yellow]No network rules configured.[/yellow]")
        console.print("  [dim]External access will be unavailable. Use [cyan]snowclaw network add <host>[/cyan] to add rules.[/dim]")
        console.print()

    # Assemble build context
    console.print("[bold]Assembling build context...[/bold]")
    build_dir = assemble_build_context(root)
    console.print(f"  [green]✓[/green] Build context ready")
    console.print()

    # Docker login
    console.print("[bold]Authenticating to Snowflake image registry...[/bold]")
    result = subprocess.run(
        ["docker", "login", registry_host, "--username", sf_user, "--password-stdin"],
        input=token,
        text=True,
    )
    if result.returncode != 0:
        console.print("[red]Docker login failed.[/red]")
        sys.exit(1)
    console.print(f"  [green]✓[/green] Logged in to {registry_host}")

    # Docker build
    console.print()
    console.print("[bold]Building Docker image...[/bold]")
    result = subprocess.run(
        ["docker", "build", "-t", f"snowclaw:{image_tag}", str(build_dir)],
    )
    if result.returncode != 0:
        console.print("[red]Build failed.[/red]")
        sys.exit(1)
    console.print(f"  [green]✓[/green] Built snowclaw:{image_tag}")

    # Docker tag & push
    full_image = f"{image_repo}/snowclaw:{image_tag}"
    console.print()
    console.print("[bold]Pushing to Snowflake image repository...[/bold]")
    subprocess.run(["docker", "tag", f"snowclaw:{image_tag}", full_image], check=True)
    result = subprocess.run(["docker", "push", full_image])
    if result.returncode != 0:
        console.print("[red]Push failed.[/red]")
        sys.exit(1)
    console.print(f"  [green]✓[/green] Pushed {full_image}")

    # Update secrets via REST API
    console.print()
    console.print("[bold]Updating Snowflake secrets...[/bold]")
    secret_map = {
        names["secret_sf_token"]: token,
        names["secret_slack_bot_token"]: env.get("SLACK_BOT_TOKEN", ""),
        names["secret_slack_app_token"]: env.get("SLACK_APP_TOKEN", ""),
        names["secret_gh_token"]: env.get("GH_TOKEN", ""),
        names["secret_brave_api_key"]: env.get("BRAVE_API_KEY", ""),
    }

    # Add channel secrets dynamically from openclaw.json
    config_path = root / "openclaw.json"
    if config_path.exists():
        import json as _json

        oc_config = _json.loads(config_path.read_text())
        enabled_channels = [
            ch for ch, cfg in oc_config.get("channels", {}).items()
            if cfg.get("enabled", False)
        ]
        prefix = re.sub(r"_db$", "", db.lower())
        for sec in get_channel_secrets(prefix, enabled_channels):
            secret_map[sec["secret_name"]] = env.get(sec["env_var"], "")

    for secret_name, value in secret_map.items():
        if value:
            escaped = value.replace("'", "\\'")
            try:
                snowflake_rest_execute(
                    account, token,
                    f"ALTER SECRET {fqn_schema}.{secret_name} SET SECRET_STRING = '{escaped}'",
                    database=db, schema=schema_name,
                )
                console.print(f"  [green]✓[/green] Updated {secret_name}")
            except requests.HTTPError as e:
                console.print(f"  [red]✗[/red] Failed to update {secret_name}: {e}")
                raise

    # Create/alter SPCS service
    console.print()
    console.print("[bold]Creating/updating SPCS service...[/bold]")
    service_spec = (build_dir / "spcs" / "service.yaml").read_text()
    service_name = names["service"]
    pool = names["pool"]
    external_access = names["external_access"]

    # REST API doesn't support $$ dollar-quoting; use single-quoted string
    escaped_spec = service_spec.replace("'", "''")
    create_sql = (
        f"CREATE SERVICE IF NOT EXISTS {fqn_schema}.{service_name} "
        f"IN COMPUTE POOL {pool} "
        f"FROM SPECIFICATION '{escaped_spec}' "
        f"EXTERNAL_ACCESS_INTEGRATIONS = ({external_access})"
    )
    try:
        snowflake_rest_execute(account, token, create_sql, database=db, schema=schema_name, warehouse=warehouse)
        console.print(f"  [green]✓[/green] CREATE SERVICE")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] CREATE SERVICE failed: {e}")
        raise

    alter_sql = (
        f"ALTER SERVICE IF EXISTS {fqn_schema}.{service_name} "
        f"FROM SPECIFICATION '{escaped_spec}'"
    )
    try:
        snowflake_rest_execute(account, token, alter_sql, database=db, schema=schema_name, warehouse=warehouse)
        console.print(f"  [green]✓[/green] ALTER SERVICE")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] ALTER SERVICE failed: {e}")
        raise

    # Show endpoints
    console.print()
    try:
        data = snowflake_rest_execute(
            account, token,
            f"SHOW ENDPOINTS IN SERVICE {fqn_schema}.{service_name}",
            database=db, schema=schema_name,
        )
        for row in data.get("data", []):
            console.print(f"  Endpoint: {row[0]} -> {row[1]}")
    except requests.HTTPError:
        console.print("  [dim]Could not retrieve endpoints.[/dim]")

    console.print()
    console.print("[green]Deployment complete.[/green]")


def cmd_update(args: argparse.Namespace):
    """Update the OpenClaw version in the .snowclaw marker."""
    render_banner()
    root = find_project_root()
    marker = read_marker(root)

    current = marker.get("openclaw_version", "latest")
    console.print(f"Current OpenClaw version: [cyan]{current}[/cyan]")

    new_version = inquirer.text(
        message="New OpenClaw version (or 'latest'):",
        default="latest",
        validate=lambda v: len(v.strip()) > 0,
    ).execute().strip()

    if new_version == current:
        console.print("[dim]Version unchanged, nothing to do.[/dim]")
        return

    marker["openclaw_version"] = new_version
    write_marker(root, marker)
    console.print(f"[green]✓[/green] Updated OpenClaw version to [cyan]{new_version}[/cyan]")
    console.print("[dim]Run [cyan]snowclaw dev[/cyan] or [cyan]snowclaw deploy[/cyan] to use the new version.[/dim]")

    redeploy = inquirer.confirm(message="Redeploy now?", default=False).execute()
    if redeploy:
        cmd_deploy(args)


def _sync_targets(args: argparse.Namespace) -> list[str]:
    """Determine which targets to sync based on CLI flags."""
    if getattr(args, "workspace_only", False):
        return ["workspace"]
    if getattr(args, "skills_only", False):
        return ["skills"]
    if getattr(args, "config_only", False):
        return ["config"]
    return ["skills", "workspace", "config"]


def cmd_pull(args: argparse.Namespace):
    """Pull skills, workspace, and/or config from SPCS stage."""
    from snowclaw.stage import get_sf_connection, pull_directory, stage_list, stage_pull_file

    render_banner()
    root = find_project_root()
    ctx = load_snowflake_context(root)

    account = ctx["account"]
    token = ctx["token"]
    sf_user = ctx["user"]
    names = ctx["names"]

    if not all([account, token, sf_user]):
        console.print("[red]Missing required credentials in .env.[/red]")
        console.print("Required: SNOWFLAKE_ACCOUNT, SNOWFLAKE_TOKEN, SNOWFLAKE_USER")
        sys.exit(1)

    targets = _sync_targets(args)
    fqn_stage = f"{names['schema']}.{names['stage']}"

    conn = get_sf_connection(
        account=account,
        user=sf_user,
        token=token,
        warehouse=ctx["warehouse"],
        database=names["db"],
        schema=names["schema_name"],
    )
    try:
        for target in targets:
            if target == "config":
                console.print("[bold]Pulling openclaw.json...[/bold]")
                # Check if openclaw.json exists on stage
                files = stage_list(conn, fqn_stage, prefix="openclaw.json")
                if not files:
                    console.print("  [dim]No openclaw.json found on stage[/dim]")
                    continue
                stage_pull_file(conn, fqn_stage, "openclaw.json", str(root))
                console.print("  [green]✓[/green] Pulled openclaw.json")
            else:
                local_dir = root / target
                local_dir.mkdir(parents=True, exist_ok=True)
                console.print(f"[bold]Pulling {target}/...[/bold]")
                downloaded = pull_directory(conn, fqn_stage, target, local_dir)
                for f in downloaded:
                    console.print(f"  [green]✓[/green] {target}/{f}")
                if not downloaded:
                    console.print(f"  [dim]No files found on stage for {target}/[/dim]")
    finally:
        conn.close()

    console.print()
    console.print("[green]Pull complete.[/green]")


def cmd_push(args: argparse.Namespace):
    """Push skills, workspace, and/or config to SPCS stage."""
    from snowclaw.stage import get_sf_connection, push_directory, stage_push_file

    render_banner()
    root = find_project_root()
    ctx = load_snowflake_context(root)

    account = ctx["account"]
    token = ctx["token"]
    sf_user = ctx["user"]
    names = ctx["names"]

    if not all([account, token, sf_user]):
        console.print("[red]Missing required credentials in .env.[/red]")
        console.print("Required: SNOWFLAKE_ACCOUNT, SNOWFLAKE_TOKEN, SNOWFLAKE_USER")
        sys.exit(1)

    targets = _sync_targets(args)
    fqn_stage = f"{names['schema']}.{names['stage']}"

    conn = get_sf_connection(
        account=account,
        user=sf_user,
        token=token,
        warehouse=ctx["warehouse"],
        database=names["db"],
        schema=names["schema_name"],
    )
    try:
        for target in targets:
            if target == "config":
                config_file = root / "openclaw.json"
                if not config_file.is_file():
                    console.print("  [dim]Skipping openclaw.json (file not found)[/dim]")
                    continue
                console.print("[bold]Pushing openclaw.json...[/bold]")
                stage_push_file(conn, fqn_stage, str(config_file), "")
                console.print("  [green]✓[/green] Pushed openclaw.json")
            else:
                local_dir = root / target
                if not local_dir.is_dir():
                    console.print(f"  [dim]Skipping {target}/ (directory not found)[/dim]")
                    continue
                console.print(f"[bold]Pushing {target}/...[/bold]")
                uploaded = push_directory(conn, fqn_stage, target, local_dir)
                for f in uploaded:
                    console.print(f"  [green]✓[/green] {target}/{f}")
                if not uploaded:
                    console.print(f"  [dim]No files to upload in {target}/[/dim]")
    finally:
        conn.close()

    console.print()
    console.print("[green]Push complete.[/green]")


# ---------------------------------------------------------------------------
# snowclaw network
# ---------------------------------------------------------------------------


def cmd_network(args: argparse.Namespace):
    """Manage network rules for SPCS external access."""
    sub = getattr(args, "network_command", None)
    if not sub:
        # Default: show current rules
        _network_list(args)
        return

    dispatch = {
        "list": _network_list,
        "add": _network_add,
        "remove": _network_remove,
        "apply": _network_apply,
        "detect": _network_detect,
    }
    handler = dispatch.get(sub)
    if handler:
        handler(args)


def _network_list(args: argparse.Namespace):
    """List current approved network rules."""
    render_banner()
    root = find_project_root()
    rules = load_network_rules(root)

    if not rules:
        console.print("[dim]No network rules configured.[/dim]")
        console.print(
            "Run [cyan]snowclaw network detect[/cyan] to auto-detect required rules,"
        )
        console.print(
            "or [cyan]snowclaw network add <host>[/cyan] to add one manually."
        )
        return

    console.print(format_rules_table(rules))
    console.print(f"\n[dim]{len(rules)} rule(s) total[/dim]")


def _network_add(args: argparse.Namespace):
    """Add a network rule."""
    render_banner()
    root = find_project_root()
    rules = load_network_rules(root)

    host_input = getattr(args, "host", None)
    if not host_input:
        console.print("[red]Usage: snowclaw network add <host[:port]>[/red]")
        return

    host, port = parse_host_port(host_input)
    reason = getattr(args, "reason", "") or ""

    # Check for duplicates
    for r in rules:
        if r.host == host and r.port == port:
            console.print(f"[yellow]Rule already exists:[/yellow] {r.host_port}")
            return

    if not reason:
        reason = inquirer.text(
            message=f"Reason for {host}:{port} (optional):",
            default="",
        ).execute().strip()

    new_rule = NetworkRule(host, port, reason)
    rules.append(new_rule)
    save_network_rules(root, rules)
    console.print(f"[green]✓[/green] Added [cyan]{new_rule.host_port}[/cyan]")

    # Offer to apply immediately
    _offer_apply(root)


def _network_remove(args: argparse.Namespace):
    """Remove a network rule."""
    render_banner()
    root = find_project_root()
    rules = load_network_rules(root)

    host_input = getattr(args, "host", None)
    if not host_input:
        console.print("[red]Usage: snowclaw network remove <host[:port]>[/red]")
        return

    host, port = parse_host_port(host_input)
    original_count = len(rules)
    rules = [r for r in rules if not (r.host == host and r.port == port)]

    if len(rules) == original_count:
        console.print(f"[yellow]No rule found matching {host}:{port}[/yellow]")
        return

    save_network_rules(root, rules)
    console.print(f"[green]✓[/green] Removed [cyan]{host}:{port}[/cyan]")

    # Offer to apply immediately
    _offer_apply(root)


def _network_apply(args: argparse.Namespace):
    """Apply current network rules to Snowflake."""
    render_banner()
    root = find_project_root()
    rules = load_network_rules(root)

    if not rules:
        console.print("[yellow]No network rules to apply.[/yellow]")
        console.print("Add rules with [cyan]snowclaw network add <host>[/cyan] first.")
        return

    console.print("[bold]Current network rules:[/bold]")
    console.print(format_rules_table(rules))
    console.print()

    approved = inquirer.confirm(
        message=f"Apply {len(rules)} rule(s) to Snowflake?",
        default=True,
    ).execute()

    if not approved:
        console.print("[dim]Aborted.[/dim]")
        return

    ctx = load_snowflake_context(root)
    if not ctx["account"] or not ctx["token"]:
        console.print("[red]Missing Snowflake credentials in .env.[/red]")
        sys.exit(1)

    success = apply_network_rules(ctx["account"], ctx["token"], ctx["names"], rules)
    if success:
        console.print()
        console.print("[green]Network rules applied to Snowflake.[/green]")
    else:
        console.print()
        console.print("[red]Failed to apply network rules.[/red]")
        sys.exit(1)


def _network_detect(args: argparse.Namespace):
    """Auto-detect required network rules from project config."""
    render_banner()
    root = find_project_root()

    current = load_network_rules(root)
    detected = detect_required_rules(root)

    if not detected:
        console.print("[dim]No external access requirements detected in config.[/dim]")
        return

    console.print("[bold]Detected network rules from project config:[/bold]")
    console.print(format_rules_table(detected, title="Detected Rules"))

    if current:
        added, removed = diff_rules(current, detected)
        if added or removed:
            console.print()
            console.print("[bold]Changes vs. current rules:[/bold]")
            print_diff(added, removed)
        else:
            console.print()
            console.print("[dim]Current rules already match detected requirements.[/dim]")
            return

    console.print()
    save_rules = inquirer.confirm(
        message="Save detected rules?",
        default=True,
    ).execute()

    if save_rules:
        if current:
            # Merge detected into current
            added, removed = diff_rules(current, detected)
            removed_set = {(r.host, r.port) for r in removed}
            merged = [r for r in current if (r.host, r.port) not in removed_set]
            existing_set = {(r.host, r.port) for r in merged}
            for r in added:
                if (r.host, r.port) not in existing_set:
                    merged.append(r)
            save_network_rules(root, merged)
        else:
            save_network_rules(root, detected)
        console.print("[green]✓[/green] Network rules saved.")
        _offer_apply(root)
    else:
        console.print("[dim]Rules not saved.[/dim]")


def cmd_status(args: argparse.Namespace):
    """Show the current state of the deployed OpenClaw instance."""
    render_banner()
    root = find_project_root()
    ctx = load_snowflake_context(root)

    account = ctx["account"]
    token = ctx["token"]
    names = ctx["names"]

    if not account or not token:
        console.print("[red]Missing Snowflake credentials in .env.[/red]")
        console.print("Required: SNOWFLAKE_ACCOUNT, SNOWFLAKE_TOKEN")
        sys.exit(1)

    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    warehouse = ctx["warehouse"]
    service_name = names["service"]
    pool_name = names["pool"]

    STATUS_COLORS = {
        "RUNNING": "[green]🟢 RUNNING[/green]",
        "READY": "[green]🟢 READY[/green]",
        "ACTIVE": "[green]🟢 ACTIVE[/green]",
        "PENDING": "[yellow]🟡 PENDING[/yellow]",
        "STARTING": "[yellow]🟡 STARTING[/yellow]",
        "IDLE": "[yellow]🟡 IDLE[/yellow]",
        "SUSPENDING": "[yellow]🟡 SUSPENDING[/yellow]",
        "RESUMING": "[yellow]🟡 RESUMING[/yellow]",
        "FAILED": "[red]🔴 FAILED[/red]",
        "SUSPENDED": "[red]🔴 SUSPENDED[/red]",
    }

    def fmt_status(status: str) -> str:
        return STATUS_COLORS.get(status.upper(), f"[dim]{status}[/dim]")

    # --- Service Status ---
    console.print(f"[bold]Service:[/bold] {service_name}")
    service_ok = False
    try:
        data = snowflake_rest_execute(
            account, token,
            f"DESCRIBE SERVICE {fqn_schema}.{service_name}",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        rows = data.get("data", [])
        if rows:
            service_ok = True
            # DESCRIBE SERVICE returns rows with service properties
            # Columns typically: name, database_name, schema_name, owner, compute_pool,
            # ... status is usually in the row data
            # We'll look at resultSetMetaData to find column positions
            columns = [
                col["name"].upper()
                for col in data.get("resultSetMetaData", {}).get("rowType", [])
            ]
            row = rows[0]
            col_map = {c: i for i, c in enumerate(columns)}

            status_val = row[col_map["STATUS"]] if "STATUS" in col_map else "UNKNOWN"
            console.print(f"[bold]Status:[/bold]  {fmt_status(status_val)}")

            if "CREATED_ON" in col_map:
                console.print(f"[bold]Created:[/bold] [dim]{row[col_map['CREATED_ON']]}[/dim]")
            if "NUM_INSTANCES" in col_map:
                console.print(f"[bold]Instances:[/bold] {row[col_map['NUM_INSTANCES']]}")
        else:
            console.print("[bold]Status:[/bold]  [red]🔴 No data returned[/red]")
    except requests.HTTPError:
        console.print("[bold]Status:[/bold]  [red]🔴 Service not found[/red]")
        console.print("[dim]Deploy with [cyan]snowclaw deploy[/cyan] first.[/dim]")

    # --- Endpoints ---
    console.print()
    if service_ok:
        try:
            data = snowflake_rest_execute(
                account, token,
                f"SHOW ENDPOINTS IN SERVICE {fqn_schema}.{service_name}",
                database=db, schema=schema_name,
            )
            rows = data.get("data", [])
            if rows:
                columns = [
                    col["name"].upper()
                    for col in data.get("resultSetMetaData", {}).get("rowType", [])
                ]
                col_map = {c: i for i, c in enumerate(columns)}
                name_idx = col_map.get("NAME", 0)
                url_idx = col_map.get("INGRESS_URL", col_map.get("URL", 1))

                console.print("[bold]Endpoints:[/bold]")
                for row in rows:
                    ep_name = row[name_idx] if name_idx < len(row) else "?"
                    ep_url = row[url_idx] if url_idx < len(row) else "?"
                    console.print(f"  {ep_name} → [cyan]{ep_url}[/cyan]")
            else:
                console.print("[bold]Endpoints:[/bold] [dim]None available yet[/dim]")
        except requests.HTTPError:
            console.print("[bold]Endpoints:[/bold] [dim]Could not retrieve endpoints[/dim]")
    else:
        console.print("[bold]Endpoints:[/bold] [dim]N/A (service not found)[/dim]")

    # --- Compute Pool ---
    console.print()
    console.print(f"[bold]Compute Pool:[/bold] {pool_name}")
    try:
        data = snowflake_rest_execute(
            account, token,
            f"DESCRIBE COMPUTE POOL {pool_name}",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        rows = data.get("data", [])
        if rows:
            columns = [
                col["name"].upper()
                for col in data.get("resultSetMetaData", {}).get("rowType", [])
            ]
            row = rows[0]
            col_map = {c: i for i, c in enumerate(columns)}

            pool_status = row[col_map["STATE"]] if "STATE" in col_map else "UNKNOWN"
            console.print(f"[bold]Status:[/bold]       {fmt_status(pool_status)}")

            if "INSTANCE_FAMILY" in col_map:
                console.print(f"[bold]Instance:[/bold]     {row[col_map['INSTANCE_FAMILY']]}")
            if "MIN_NODES" in col_map and "MAX_NODES" in col_map:
                min_n = row[col_map["MIN_NODES"]]
                max_n = row[col_map["MAX_NODES"]]
                console.print(f"[bold]Nodes:[/bold]        {min_n}/{max_n} (min/max)")
            if "NUM_SERVICES" in col_map:
                console.print(f"[bold]Services:[/bold]     {row[col_map['NUM_SERVICES']]}")
    except requests.HTTPError:
        console.print("[bold]Status:[/bold]       [red]🔴 Compute pool not found[/red]")

    console.print()


def _offer_apply(root: Path):
    """Ask whether to apply rules to Snowflake now."""
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

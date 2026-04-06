"""CLI command implementations."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from InquirerPy import inquirer
from rich.panel import Panel

from snowclaw import __version__
from snowclaw.config import CORTEX_MODELS, write_connections_toml, write_dotenv, write_openclaw_config
from snowclaw.channels import (
    channel_add,
    channel_edit,
    channel_list,
    channel_remove,
)
from snowclaw.network import (
    CHANNEL_REGISTRY,
    TOOL_REGISTRY,
    NetworkRule,
    apply_network_rules,
    detect_required_rules,
    diff_rules,
    format_rules_table,
    get_channel_secrets,
    get_env_secrets,
    load_network_rules,
    offer_apply_rules,
    parse_host_port,
    print_diff,
    save_network_rules,
)
from snowclaw.scaffold import assemble_build_context, assemble_proxy_build_context, scaffold_user_files
from snowclaw.snowflake import run_proxy_snowflake_setup, run_snowflake_setup
from snowclaw.utils import (
    console,
    find_project_root,
    get_templates_dir,
    load_snowflake_context,
    read_marker,
    render_banner,
    sf_names,
    sf_proxy_names,
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
            if cred.get("hint"):
                console.print(f"  [dim]{cred['hint']}[/dim]")
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

    # --- Default model ---
    default_model = inquirer.select(
        message="Default model for your agent:",
        choices=[
            {"name": f"{m['name']} (Recommended)" if i == 0 else m["name"], "value": m["id"]}
            for i, m in enumerate(CORTEX_MODELS)
        ],
    ).execute()

    warehouse = inquirer.text(message="Snowflake warehouse:", default="COMPUTE_WH").execute()
    role = inquirer.text(message="Snowflake role:", default="SYSADMIN").execute()
    console.print(
        "\n[dim]SnowClaw service objects (image repo, stage, compute pool, secrets, etc.) "
        "will be created in this database and schema. You can use an existing database/schema "
        "as long as the role above has the required privileges.[/dim]\n"
    )
    database = inquirer.text(
        message="Snowflake database:",
        default="snowclaw_db",
        validate=lambda v: bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", v.strip())),
        invalid_message="Database name must be alphanumeric with underscores, starting with a letter.",
    ).execute().strip()
    schema = inquirer.text(
        message="Snowflake schema:",
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
        "default_model": default_model,
        "tools": tools,
        "tool_credentials": tool_credentials,
    }

    # --- Write .snowclaw marker ---
    marker = {
        "version": __version__,
        "created": datetime.now(timezone.utc).isoformat(),
        "account": account.strip(),
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
        "  [cyan]snowclaw channel add[/cyan]     — add Slack, Telegram, or Discord\n"
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

    console.print("[bold]Building proxy image...[/bold]")
    result = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", f"snowclaw-proxy:{image_tag}", str(build_dir / "proxy")],
    )
    if result.returncode != 0:
        console.print("[red]Proxy build failed.[/red]")
        sys.exit(result.returncode)
    console.print(f"[green]✓[/green] Built image [cyan]snowclaw-proxy:{image_tag}[/cyan]")

    console.print()
    console.print("[bold]Building Docker image...[/bold]")
    result = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", f"snowclaw:{image_tag}", str(build_dir)],
    )
    if result.returncode != 0:
        console.print("[red]Build failed.[/red]")
        sys.exit(result.returncode)

    console.print()
    console.print(f"[green]✓[/green] Built image [cyan]snowclaw:{image_tag}[/cyan]")


def _update_secrets(root: Path, ctx: dict, names: dict, env: dict) -> None:
    """Create/update all Snowflake SECRET objects from .env values."""
    account = ctx["account"]
    token = ctx["token"]
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]

    secret_map = {
        names["secret_sf_token"]: token,
        names["secret_gh_token"]: env.get("GH_TOKEN", ""),
        names["secret_brave_api_key"]: env.get("BRAVE_API_KEY", ""),
    }

    # Add channel secrets dynamically from openclaw.json
    config_path = root / "openclaw.json"
    if config_path.exists():
        oc_config = json.loads(config_path.read_text())
        enabled_channels = [
            ch for ch, cfg in oc_config.get("channels", {}).items()
            if cfg.get("enabled", False)
        ]
        prefix = re.sub(r"_db$", "", db.lower())
        for sec in get_channel_secrets(prefix, enabled_channels):
            secret_map[sec["secret_name"]] = env.get(sec["env_var"], "")

    # Add remaining env secrets (everything not already handled above)
    prefix = re.sub(r"_db$", "", db.lower())
    for sec in get_env_secrets(prefix, root / ".env"):
        secret_map[sec["secret_name"]] = env.get(sec["env_var"], "")

    for secret_name, value in secret_map.items():
        escaped = value.replace("'", "\\'") if value else ""
        try:
            snowflake_rest_execute(
                account, token,
                f"CREATE OR REPLACE SECRET {fqn_schema}.{secret_name} "
                f"TYPE = GENERIC_STRING SECRET_STRING = '{escaped}'",
                database=db, schema=schema_name,
            )
            console.print(f"  [green]✓[/green] Updated {secret_name}")
        except requests.HTTPError as e:
            console.print(f"  [red]✗[/red] Failed to update {secret_name}: {e}")
            raise


def _upload_connections_toml(root: Path, ctx: dict, names: dict) -> None:
    """Upload connections.toml to the SPCS stage."""
    from snowclaw.stage import get_sf_connection, stage_push_file

    connections_file = root / "connections.toml"
    if not connections_file.is_file():
        console.print("  [dim]Skipping connections.toml (file not found)[/dim]")
        return

    account = ctx["account"]
    token = ctx["token"]
    sf_user = ctx["user"]
    warehouse = ctx["warehouse"]
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]

    console.print("[bold]Uploading connections.toml to stage...[/bold]")
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        conn = get_sf_connection(
            account=account,
            user=sf_user,
            token=token,
            warehouse=warehouse,
            database=db,
            schema=schema_name,
        )
        try:
            stage_push_file(conn, f"{fqn_schema}.{names['stage']}", str(connections_file), "")
            console.print(f"  [green]✓[/green] Pushed connections.toml")
            break
        except Exception as e:
            if attempt < max_attempts:
                delay = 2 ** attempt
                console.print(f"  [yellow]⚠[/yellow] Attempt {attempt}/{max_attempts} failed: {e}")
                console.print(f"  Retrying in {delay}s...")
                time.sleep(delay)
            else:
                console.print(f"  [red]✗[/red] Failed to push connections.toml after {max_attempts} attempts: {e}")
                sys.exit(1)
        finally:
            conn.close()


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
    registry_host = f"{account}.registry.snowflakecomputing.com".lower()
    image_repo = f"{registry_host}/{db}/{schema_name}/{repo}".lower()

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

    # Docker build — proxy
    console.print()
    console.print("[bold]Building proxy image...[/bold]")
    result = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", f"snowclaw-proxy:{image_tag}", str(build_dir / "proxy")],
    )
    if result.returncode != 0:
        console.print("[red]Proxy build failed.[/red]")
        sys.exit(1)
    console.print(f"  [green]✓[/green] Built snowclaw-proxy:{image_tag}")

    # Docker build — main
    console.print()
    console.print("[bold]Building Docker image...[/bold]")
    result = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", f"snowclaw:{image_tag}", str(build_dir)],
    )
    if result.returncode != 0:
        console.print("[red]Build failed.[/red]")
        sys.exit(1)
    console.print(f"  [green]✓[/green] Built snowclaw:{image_tag}")

    # Docker tag & push — proxy
    full_proxy_image = f"{image_repo}/snowclaw-proxy:{image_tag}"
    console.print()
    console.print("[bold]Pushing proxy to Snowflake image repository...[/bold]")
    subprocess.run(["docker", "tag", f"snowclaw-proxy:{image_tag}", full_proxy_image], check=True)
    result = subprocess.run(["docker", "push", full_proxy_image])
    if result.returncode != 0:
        console.print("[red]Proxy push failed.[/red]")
        sys.exit(1)
    console.print(f"  [green]✓[/green] Pushed {full_proxy_image}")

    # Docker tag & push — main
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
    _update_secrets(root, ctx, names, env)

    # Upload openclaw.json to stage (config lives on the volume, not in the image)
    config_file = root / "openclaw.json"
    if config_file.is_file():
        from snowclaw.stage import get_sf_connection, stage_push_file

        console.print()
        console.print("[bold]Uploading config to stage...[/bold]")
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            conn = get_sf_connection(
                account=account,
                user=sf_user,
                token=token,
                warehouse=warehouse,
                database=db,
                schema=schema_name,
            )
            try:
                stage_push_file(conn, f"{fqn_schema}.{names['stage']}", str(config_file), "")
                console.print(f"  [green]✓[/green] Pushed openclaw.json")
                break
            except Exception as e:
                if attempt < max_attempts:
                    delay = 2 ** attempt
                    console.print(f"  [yellow]⚠[/yellow] Attempt {attempt}/{max_attempts} failed: {e}")
                    console.print(f"  Retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    console.print(f"  [red]✗[/red] Failed to push openclaw.json after {max_attempts} attempts: {e}")
                    sys.exit(1)
            finally:
                conn.close()

    # Upload connections.toml to stage
    console.print()
    _upload_connections_toml(root, ctx, names)

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

    alter_spec_sql = (
        f"ALTER SERVICE IF EXISTS {fqn_schema}.{service_name} "
        f"FROM SPECIFICATION '{escaped_spec}'"
    )
    try:
        snowflake_rest_execute(account, token, alter_spec_sql, database=db, schema=schema_name, warehouse=warehouse)
        console.print(f"  [green]✓[/green] ALTER SERVICE (spec)")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] ALTER SERVICE (spec) failed: {e}")
        raise

    alter_eai_sql = (
        f"ALTER SERVICE IF EXISTS {fqn_schema}.{service_name} "
        f"SET EXTERNAL_ACCESS_INTEGRATIONS = ({external_access})"
    )
    try:
        snowflake_rest_execute(account, token, alter_eai_sql, database=db, schema=schema_name, warehouse=warehouse)
        console.print(f"  [green]✓[/green] ALTER SERVICE (external access)")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] ALTER SERVICE (external access) failed: {e}")
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
    env = ctx["env"]

    if not all([account, token, sf_user]):
        console.print("[red]Missing required credentials in .env.[/red]")
        console.print("Required: SNOWFLAKE_ACCOUNT, SNOWFLAKE_TOKEN, SNOWFLAKE_USER")
        sys.exit(1)

    secrets_only = getattr(args, "secrets", False)
    has_target_flag = any(
        getattr(args, f, False) for f in ("workspace_only", "skills_only", "config_only")
    )

    # Push targets unless --secrets is used alone
    skip_targets = secrets_only and not has_target_flag
    if not skip_targets:
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

    # Always update secrets and upload connections.toml
    console.print()
    console.print("[bold]Updating Snowflake secrets...[/bold]")
    _update_secrets(root, ctx, names, env)

    console.print()
    _upload_connections_toml(root, ctx, names)

    # Regenerate service spec and alter service to pick up secret changes
    console.print()
    console.print("[bold]Updating service specification...[/bold]")
    build_dir = assemble_build_context(root)
    service_spec = (build_dir / "spcs" / "service.yaml").read_text()
    escaped_spec = service_spec.replace("'", "''")
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    warehouse = ctx["warehouse"]
    service_name = names["service"]
    service_fqn = f"{fqn_schema}.{service_name}"

    external_access = names["external_access"]
    alter_spec_sql = (
        f"ALTER SERVICE IF EXISTS {service_fqn} "
        f"FROM SPECIFICATION '{escaped_spec}'"
    )
    try:
        snowflake_rest_execute(account, token, alter_spec_sql, database=db, schema=schema_name, warehouse=warehouse)
        console.print(f"  [green]✓[/green] ALTER SERVICE (spec)")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] ALTER SERVICE (spec) failed: {e}")
        raise

    alter_eai_sql = (
        f"ALTER SERVICE IF EXISTS {service_fqn} "
        f"SET EXTERNAL_ACCESS_INTEGRATIONS = ({external_access})"
    )
    try:
        snowflake_rest_execute(account, token, alter_eai_sql, database=db, schema=schema_name, warehouse=warehouse)
        console.print(f"  [green]✓[/green] ALTER SERVICE (external access)")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] ALTER SERVICE (external access) failed: {e}")
        raise

    # Restart service (suspend + resume) to apply changes
    console.print()
    console.print("[bold]Restarting service to apply updated secrets and configuration...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER SERVICE {service_fqn} SUSPEND",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Service suspended")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to suspend service: {e}")
        sys.exit(1)

    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER SERVICE {service_fqn} RESUME",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Service resumed")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to resume service: {e}")
        sys.exit(1)

    console.print()
    console.print("[green]Push complete — service restarting with updated secrets and configuration.[/green]")
    console.print(
        "[yellow]Note:[/yellow] The container may take a minute or two to fully spin up."
    )


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
    offer_apply_rules(root)


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
    offer_apply_rules(root)


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
        offer_apply_rules(root)
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
                    link = ep_url if ep_url.startswith("http") else f"https://{ep_url}"
                    console.print(f"  {ep_name} → [link={link}][cyan]{ep_url}[/cyan][/link]")
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


def cmd_suspend(args: argparse.Namespace):
    """Suspend the SPCS service and compute pool."""
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

    # Suspend service first (required before suspending compute pool)
    console.print(f"[bold]Suspending service {service_name}...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER SERVICE {fqn_schema}.{service_name} SUSPEND",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Service suspended")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to suspend service: {e}")
        sys.exit(1)

    # Suspend compute pool
    console.print(f"[bold]Suspending compute pool {pool_name}...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER COMPUTE POOL {pool_name} SUSPEND",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Compute pool suspended")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to suspend compute pool: {e}")
        sys.exit(1)

    console.print()
    console.print("[green]Suspend complete.[/green]")


def cmd_resume(args: argparse.Namespace):
    """Resume the SPCS compute pool and service."""
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

    # Resume compute pool first (required before resuming service)
    console.print(f"[bold]Resuming compute pool {pool_name}...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER COMPUTE POOL {pool_name} RESUME",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Compute pool resumed")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to resume compute pool: {e}")
        sys.exit(1)

    # Resume service
    console.print(f"[bold]Resuming service {service_name}...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER SERVICE {fqn_schema}.{service_name} RESUME",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Service resumed")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to resume service: {e}")
        sys.exit(1)

    console.print()
    console.print("[green]Resume complete.[/green]")


def cmd_restart(args: argparse.Namespace):
    """Restart the SPCS service (suspend then resume) to pick up config changes."""
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
    service_fqn = f"{fqn_schema}.{service_name}"

    console.print(f"[bold]Restarting service {service_name}...[/bold]")

    # Suspend service
    console.print("  Suspending...")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER SERVICE {service_fqn} SUSPEND",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Service suspended")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to suspend service: {e}")
        sys.exit(1)

    # Resume service
    console.print("  Resuming...")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER SERVICE {service_fqn} RESUME",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Service resumed")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to resume service: {e}")
        sys.exit(1)

    console.print()
    console.print("[green]Restart complete — gateway will reload config on startup.[/green]")
    console.print(
        "[yellow]Note:[/yellow] The container may take a minute or two to fully spin up."
    )


def cmd_channel(args: argparse.Namespace):
    """Manage communication channel configurations."""
    sub = getattr(args, "channel_command", None)
    if not sub:
        channel_list()
        return

    dispatch = {
        "list": lambda a: channel_list(),
        "add": lambda a: channel_add(),
        "remove": lambda a: channel_remove(getattr(a, "name", None)),
        "edit": lambda a: channel_edit(getattr(a, "name", None)),
    }
    handler = dispatch.get(sub)
    if handler:
        handler(args)


def cmd_model(args: argparse.Namespace):
    """View or change the default agent model."""
    sub = getattr(args, "model_command", None)
    if not sub:
        model_show()
        return

    dispatch = {
        "list": lambda a: model_list(),
        "set": lambda a: model_set(),
    }
    handler = dispatch.get(sub)
    if handler:
        handler(args)


def model_show():
    """Show the current default model."""
    root = find_project_root()
    config_path = root / "openclaw.json"
    if not config_path.exists():
        console.print("[red]No openclaw.json found. Run snowclaw setup first.[/red]")
        sys.exit(1)

    config = json.loads(config_path.read_text())
    current = config.get("agents", {}).get("defaults", {}).get("model", "not set")
    console.print(f"Current default model: [bold]{current}[/bold]")


def model_list():
    """List available Cortex models and highlight the current default."""
    root = find_project_root()
    config_path = root / "openclaw.json"
    current = ""
    if config_path.exists():
        config = json.loads(config_path.read_text())
        current = config.get("agents", {}).get("defaults", {}).get("model", "")

    console.print("[bold]Available Cortex models:[/bold]\n")
    for m in CORTEX_MODELS:
        prefixed = f"cortex:{m['id']}"
        marker = " [green]← current[/green]" if prefixed == current else ""
        console.print(f"  {m['name']} [dim]({m['id']})[/dim]{marker}")
    console.print()


def model_set():
    """Interactively change the default model in openclaw.json."""
    root = find_project_root()
    config_path = root / "openclaw.json"
    if not config_path.exists():
        console.print("[red]No openclaw.json found. Run snowclaw setup first.[/red]")
        sys.exit(1)

    config = json.loads(config_path.read_text())
    current = config.get("agents", {}).get("defaults", {}).get("model", "")

    selected = inquirer.select(
        message="Select default model:",
        choices=[
            {"name": m["name"], "value": m["id"]}
            for m in CORTEX_MODELS
        ],
        default=current.removeprefix("cortex/") if current.startswith("cortex/") else None,
    ).execute()

    config.setdefault("agents", {}).setdefault("defaults", {})["model"] = f"cortex/{selected}"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    model_name = next((m["name"] for m in CORTEX_MODELS if m["id"] == selected), selected)
    console.print(f"  [green]✓[/green] Default model set to [bold]{model_name}[/bold]")


def cmd_logs(args: argparse.Namespace):
    """Fetch and display container logs from the SPCS service."""
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

    num_lines = getattr(args, "lines", 100)
    container = "cortex-proxy" if getattr(args, "proxy", False) else getattr(args, "container", "openclaw")
    instance_id = getattr(args, "instance", "0")

    fqn_service = f"{fqn_schema}.{service_name}"
    sql = (
        f"CALL SYSTEM$GET_SERVICE_LOGS("
        f"'{fqn_service}', '{instance_id}', '{container}', {num_lines})"
    )

    console.print(
        f"[bold]Fetching logs:[/bold] {service_name} "
        f"[dim](container={container}, instance={instance_id}, lines={num_lines})[/dim]"
    )
    console.print()

    try:
        data = snowflake_rest_execute(
            account, token, sql,
            database=db, schema=schema_name, warehouse=warehouse,
        )
        rows = data.get("data", [])
        if rows and rows[0]:
            log_text = rows[0][0]
            if log_text:
                console.print(log_text)
            else:
                console.print("[dim]No log output returned.[/dim]")
        else:
            console.print("[dim]No log output returned.[/dim]")
    except requests.HTTPError as e:
        console.print(f"[red]Failed to fetch logs:[/red] {e}")
        sys.exit(1)


def cmd_upgrade(args: argparse.Namespace):
    """Update SnowClaw CLI to the latest version via git pull + pipx reinstall."""
    render_banner()

    old_version = __version__

    # Find the repo root by walking up from this file
    repo_dir = Path(__file__).resolve().parent
    while repo_dir != repo_dir.parent:
        if (repo_dir / ".git").is_dir():
            break
        repo_dir = repo_dir.parent
    else:
        console.print("[red]SnowClaw was not installed from a git repository.[/red]")
        sys.exit(1)

    # Warn if working tree is dirty
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if status_result.stdout.strip():
        console.print("[yellow]Warning: SnowClaw repo has uncommitted changes.[/yellow]")

    # Pull latest changes
    console.print("Pulling latest changes...")
    pull_result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if pull_result.returncode != 0:
        console.print(f"[red]git pull failed:[/red]\n{pull_result.stderr.strip()}")
        sys.exit(1)

    if "Already up to date" in pull_result.stdout:
        console.print(f"[green]✓[/green] Already up to date (snowclaw {old_version})")
        return

    console.print(pull_result.stdout.strip())

    # Find python executable
    python_cmd = None
    for candidate in ("python3", "python"):
        check = subprocess.run(
            [candidate, "--version"], capture_output=True, text=True,
        )
        if check.returncode == 0:
            python_cmd = candidate
            break
    if not python_cmd:
        console.print("[red]Could not find python3 or python on PATH.[/red]")
        sys.exit(1)

    # Reinstall via pipx
    console.print("Reinstalling with pipx...")
    pipx_result = subprocess.run(
        [python_cmd, "-m", "pipx", "install", "--force", "-e", str(repo_dir)],
        capture_output=True, text=True,
    )
    if pipx_result.returncode != 0:
        console.print(f"[red]pipx reinstall failed:[/red]\n{pipx_result.stderr.strip()}")
        sys.exit(1)

    # Get new version from the reinstalled CLI
    ver_result = subprocess.run(
        ["snowclaw", "--version"], capture_output=True, text=True,
    )
    new_version = ver_result.stdout.strip().removeprefix("snowclaw ") if ver_result.returncode == 0 else "unknown"

    if new_version == old_version:
        console.print(f"[green]✓[/green] Reinstalled snowclaw {old_version} (version unchanged)")
    else:
        console.print(f"[green]✓[/green] Updated snowclaw: {old_version} → {new_version}")


# ---------------------------------------------------------------------------
# snowclaw proxy — standalone Cortex proxy deployment
# ---------------------------------------------------------------------------


def _load_proxy_context(root: Path) -> dict:
    """Load Snowflake context using proxy-specific object names."""
    import os as _os
    from snowclaw.utils import load_dotenv, load_connections_toml

    marker = read_marker(root)
    env = {**_os.environ, **load_dotenv(root / ".env")}
    conn = load_connections_toml(root / "connections.toml")

    database = marker.get("database", env.get("SNOWCLAW_DB", "snowclaw_db"))
    schema = marker.get("schema", env.get("SNOWCLAW_SCHEMA", "snowclaw_schema"))
    names = sf_proxy_names(database, schema)

    return {
        "account": env.get("SNOWFLAKE_ACCOUNT"),
        "token": env.get("SNOWFLAKE_TOKEN"),
        "user": env.get("SNOWFLAKE_USER"),
        "database": database,
        "schema": schema,
        "warehouse": env.get("SNOWFLAKE_WAREHOUSE") or conn.get("warehouse"),
        "names": names,
        "env": env,
    }


def _proxy_require_creds(ctx: dict):
    """Exit if required Snowflake credentials are missing."""
    if not ctx["account"] or not ctx["token"]:
        console.print("[red]Missing Snowflake credentials in .env.[/red]")
        console.print("Required: SNOWFLAKE_ACCOUNT, SNOWFLAKE_TOKEN")
        sys.exit(1)


def _proxy_setup(args: argparse.Namespace):
    """Interactive setup wizard for standalone Cortex proxy."""
    render_banner()
    cwd = Path.cwd()

    # Refuse to scaffold inside the CLI repo itself
    cli_repo = get_templates_dir().parent
    if cwd.resolve() == cli_repo.resolve():
        console.print("[red]Cannot run proxy setup inside the snowclaw CLI repo.[/red]")
        console.print("Create a new directory and run [cyan]snowclaw proxy setup[/cyan] there:")
        console.print("  [dim]mkdir my-proxy && cd my-proxy && snowclaw proxy setup[/dim]")
        sys.exit(1)

    console.print(
        "[bold]Standalone Cortex Proxy Setup[/bold]\n"
        "[dim]This deploys a lightweight proxy on SPCS that external OpenClaw agents\n"
        "can connect to for Cortex LLM access.[/dim]\n"
    )

    # --- Collect inputs ---
    account = inquirer.text(
        message="Snowflake account identifier (orgname-accountname):",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="Account identifier is required.",
    ).execute().strip()

    sf_user = inquirer.text(
        message="Snowflake username:",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="Username is required.",
    ).execute().strip()

    pat = inquirer.secret(
        message="Programmatic access token (PAT):",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="PAT is required.",
    ).execute().strip()

    warehouse = inquirer.text(message="Snowflake warehouse:", default="COMPUTE_WH").execute().strip()
    role = inquirer.text(message="Snowflake role:", default="SYSADMIN").execute().strip()

    console.print(
        "\n[dim]Proxy service objects (image repo, compute pool, network rules) "
        "will be created in this database and schema.[/dim]\n"
    )
    database = inquirer.text(
        message="Snowflake database:",
        default="snowclaw_db",
        validate=lambda v: bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", v.strip())),
        invalid_message="Database name must be alphanumeric with underscores, starting with a letter.",
    ).execute().strip()
    schema = inquirer.text(
        message="Snowflake schema:",
        default="snowclaw_schema",
        validate=lambda v: bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", v.strip())),
        invalid_message="Schema name must be alphanumeric with underscores, starting with a letter.",
    ).execute().strip()

    # --- Write .snowclaw marker ---
    root = cwd
    marker = {
        "version": __version__,
        "created": datetime.now(timezone.utc).isoformat(),
        "account": account,
        "database": database,
        "schema": schema,
        "mode": "proxy",
    }
    write_marker(root, marker)

    # --- Write minimal .env ---
    env_lines = [
        f"SNOWFLAKE_ACCOUNT={account}",
        f"SNOWFLAKE_USER={sf_user}",
        f"SNOWFLAKE_TOKEN={pat}",
        f"SNOWFLAKE_WAREHOUSE={warehouse}",
        f"SNOWCLAW_ROLE={role}",
    ]
    (root / ".env").write_text("\n".join(env_lines) + "\n")
    console.print("[green]✓[/green] Wrote .env")

    # --- Write .gitignore ---
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".env\n.snowclaw/build-proxy/\n")
        console.print("[green]✓[/green] Wrote .gitignore")

    # --- Optionally create Snowflake objects ---
    console.print()
    create_objects = inquirer.confirm(
        message="Create Snowflake objects now? (database, schema, compute pool, network rules)",
        default=True,
    ).execute()

    if create_objects:
        console.print()
        settings = {
            "account": account,
            "pat": pat,
            "database": database,
            "schema": schema,
        }
        try:
            run_proxy_snowflake_setup(settings)
            console.print()
            console.print("[green]Snowflake objects created successfully.[/green]")
        except Exception:
            console.print("[yellow]Some objects may not have been created. You can retry or create them manually.[/yellow]")

    # --- Summary ---
    console.print()
    console.print(Panel(
        "[bold]Proxy setup complete![/bold]\n\n"
        "Next steps:\n"
        "  [cyan]snowclaw proxy deploy[/cyan]    — build and deploy the proxy to SPCS\n"
        "  [cyan]snowclaw proxy status[/cyan]    — check service status and get the endpoint URL\n\n"
        "Once deployed, configure your external OpenClaw agent:\n"
        '  Set provider baseUrl to the proxy\'s public endpoint URL\n'
        "  Set apiKey to your Snowflake PAT\n",
        title="What's next",
        border_style="green",
        expand=False,
    ))


def _proxy_deploy(args: argparse.Namespace):
    """Build, push, and deploy standalone proxy to SPCS."""
    render_banner()
    root = find_project_root()
    ctx = _load_proxy_context(root)
    _proxy_require_creds(ctx)

    account = ctx["account"]
    token = ctx["token"]
    sf_user = ctx["user"]
    names = ctx["names"]
    env = ctx["env"]

    if not sf_user:
        console.print("[red]Missing SNOWFLAKE_USER in .env.[/red]")
        sys.exit(1)

    image_tag = env.get("IMAGE_TAG", "latest")
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    warehouse = ctx["warehouse"]
    repo = names["repo"]
    registry_host = f"{account}.registry.snowflakecomputing.com".lower()
    image_repo = f"{registry_host}/{db}/{schema_name}/{repo}".lower()

    # Assemble proxy build context
    console.print("[bold]Assembling proxy build context...[/bold]")
    build_dir = assemble_proxy_build_context(root)
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

    # Build proxy image
    console.print()
    console.print("[bold]Building proxy image...[/bold]")
    result = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", f"snowclaw-proxy:{image_tag}", str(build_dir / "proxy")],
    )
    if result.returncode != 0:
        console.print("[red]Proxy build failed.[/red]")
        sys.exit(1)
    console.print(f"  [green]✓[/green] Built snowclaw-proxy:{image_tag}")

    # Tag and push
    full_proxy_image = f"{image_repo}/snowclaw-proxy:{image_tag}"
    console.print()
    console.print("[bold]Pushing proxy to Snowflake image repository...[/bold]")
    subprocess.run(["docker", "tag", f"snowclaw-proxy:{image_tag}", full_proxy_image], check=True)
    result = subprocess.run(["docker", "push", full_proxy_image])
    if result.returncode != 0:
        console.print("[red]Proxy push failed.[/red]")
        sys.exit(1)
    console.print(f"  [green]✓[/green] Pushed {full_proxy_image}")

    # Create/alter SPCS service
    console.print()
    console.print("[bold]Creating/updating SPCS proxy service...[/bold]")
    service_spec = (build_dir / "spcs" / "proxy-service.yaml").read_text()
    service_name = names["service"]
    pool = names["pool"]
    external_access = names["external_access"]

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

    alter_spec_sql = (
        f"ALTER SERVICE IF EXISTS {fqn_schema}.{service_name} "
        f"FROM SPECIFICATION '{escaped_spec}'"
    )
    try:
        snowflake_rest_execute(account, token, alter_spec_sql, database=db, schema=schema_name, warehouse=warehouse)
        console.print(f"  [green]✓[/green] ALTER SERVICE (spec)")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] ALTER SERVICE (spec) failed: {e}")
        raise

    alter_eai_sql = (
        f"ALTER SERVICE IF EXISTS {fqn_schema}.{service_name} "
        f"SET EXTERNAL_ACCESS_INTEGRATIONS = ({external_access})"
    )
    try:
        snowflake_rest_execute(account, token, alter_eai_sql, database=db, schema=schema_name, warehouse=warehouse)
        console.print(f"  [green]✓[/green] ALTER SERVICE (external access)")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] ALTER SERVICE (external access) failed: {e}")
        raise

    # Show endpoints
    console.print()
    endpoint_url = None
    console.print("[bold]Proxy endpoint:[/bold]")
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
            url_idx = col_map.get("INGRESS_URL", col_map.get("URL", 1))

            for row in rows:
                ep_url = row[url_idx] if url_idx < len(row) else "?"
                link = ep_url if ep_url.startswith("http") else f"https://{ep_url}"
                endpoint_url = link
                console.print(f"  [link={link}][cyan]{ep_url}[/cyan][/link]")
        else:
            console.print("  [dim]Endpoint not yet available. Check [cyan]snowclaw proxy status[/cyan] shortly.[/dim]")
    except requests.HTTPError:
        console.print("  [dim]Could not retrieve endpoints yet.[/dim]")

    console.print()
    console.print("[green]Proxy deployment complete.[/green]")

    # Print OpenClaw provider config snippet
    if endpoint_url:
        base_url = f"{endpoint_url}/v1"
    else:
        base_url = "https://<proxy-endpoint>/v1"
    console.print()
    console.print("[bold]Add this to your openclaw.json to connect through the proxy:[/bold]\n")
    config_json = (
        '{\n'
        '  "models": {\n'
        '    "providers": {\n'
        '      "cortex": {\n'
        f'        "baseUrl": "{base_url}",\n'
        '        "apiKey": "$SNOWFLAKE_TOKEN",\n'
        '        "headers": {\n'
        '          "Authorization": "Snowflake Token=\\"$SNOWFLAKE_TOKEN\\"",\n'
        '          "X-Cortex-Token": "$SNOWFLAKE_TOKEN"\n'
        '        },\n'
        '        "api": "openai-completions"\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}'
    )
    console.print(config_json)
    if not endpoint_url:
        console.print(
            "\n[dim]Endpoint URL is still provisioning."
            " Run [cyan]snowclaw proxy status[/cyan] to get it once ready.[/dim]"
        )
    console.print(
        '\n[dim]Each user authenticates with their own PAT.\n'
        'Authorization handles SPCS ingress auth, while X-Cortex-Token\n'
        'is passed through to Cortex for the actual LLM request.[/dim]'
    )


def _proxy_status(args: argparse.Namespace):
    """Show standalone proxy service status and endpoint."""
    render_banner()
    root = find_project_root()
    ctx = _load_proxy_context(root)
    _proxy_require_creds(ctx)

    account = ctx["account"]
    token = ctx["token"]
    names = ctx["names"]
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    warehouse = ctx["warehouse"]
    service_name = names["service"]
    pool_name = names["pool"]

    STATUS_COLORS = {
        "RUNNING": "[green]RUNNING[/green]",
        "READY": "[green]READY[/green]",
        "ACTIVE": "[green]ACTIVE[/green]",
        "PENDING": "[yellow]PENDING[/yellow]",
        "STARTING": "[yellow]STARTING[/yellow]",
        "IDLE": "[yellow]IDLE[/yellow]",
        "SUSPENDING": "[yellow]SUSPENDING[/yellow]",
        "RESUMING": "[yellow]RESUMING[/yellow]",
        "FAILED": "[red]FAILED[/red]",
        "SUSPENDED": "[red]SUSPENDED[/red]",
    }

    def fmt_status(status: str) -> str:
        return STATUS_COLORS.get(status.upper(), f"[dim]{status}[/dim]")

    # --- Service Status ---
    console.print(f"[bold]Proxy Service:[/bold] {service_name}")
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
        else:
            console.print("[bold]Status:[/bold]  [red]No data returned[/red]")
    except requests.HTTPError:
        console.print("[bold]Status:[/bold]  [red]Service not found[/red]")
        console.print("[dim]Deploy with [cyan]snowclaw proxy deploy[/cyan] first.[/dim]")

    # --- Endpoint ---
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
                url_idx = col_map.get("INGRESS_URL", col_map.get("URL", 1))

                console.print("[bold]Endpoint:[/bold]")
                for row in rows:
                    ep_url = row[url_idx] if url_idx < len(row) else "?"
                    link = ep_url if ep_url.startswith("http") else f"https://{ep_url}"
                    console.print(f"  [link={link}][cyan]{ep_url}[/cyan][/link]")
            else:
                console.print("[bold]Endpoint:[/bold] [dim]Not available yet[/dim]")
        except requests.HTTPError:
            console.print("[bold]Endpoint:[/bold] [dim]Could not retrieve[/dim]")
    else:
        console.print("[bold]Endpoint:[/bold] [dim]N/A (service not found)[/dim]")

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
    except requests.HTTPError:
        console.print("[bold]Status:[/bold]       [red]Compute pool not found[/red]")

    console.print()


def _proxy_suspend(args: argparse.Namespace):
    """Suspend the standalone proxy service and compute pool."""
    render_banner()
    root = find_project_root()
    ctx = _load_proxy_context(root)
    _proxy_require_creds(ctx)

    account = ctx["account"]
    token = ctx["token"]
    names = ctx["names"]
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    warehouse = ctx["warehouse"]
    service_name = names["service"]
    pool_name = names["pool"]

    console.print(f"[bold]Suspending proxy service {service_name}...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER SERVICE {fqn_schema}.{service_name} SUSPEND",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Service suspended")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to suspend service: {e}")
        sys.exit(1)

    console.print(f"[bold]Suspending compute pool {pool_name}...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER COMPUTE POOL {pool_name} SUSPEND",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Compute pool suspended")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to suspend compute pool: {e}")
        sys.exit(1)

    console.print()
    console.print("[green]Proxy suspended.[/green]")


def _proxy_resume(args: argparse.Namespace):
    """Resume the standalone proxy compute pool and service."""
    render_banner()
    root = find_project_root()
    ctx = _load_proxy_context(root)
    _proxy_require_creds(ctx)

    account = ctx["account"]
    token = ctx["token"]
    names = ctx["names"]
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    warehouse = ctx["warehouse"]
    service_name = names["service"]
    pool_name = names["pool"]

    console.print(f"[bold]Resuming compute pool {pool_name}...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER COMPUTE POOL {pool_name} RESUME",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Compute pool resumed")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to resume compute pool: {e}")
        sys.exit(1)

    console.print(f"[bold]Resuming proxy service {service_name}...[/bold]")
    try:
        snowflake_rest_execute(
            account, token,
            f"ALTER SERVICE {fqn_schema}.{service_name} RESUME",
            database=db, schema=schema_name, warehouse=warehouse,
        )
        console.print(f"  [green]✓[/green] Service resumed")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to resume service: {e}")
        sys.exit(1)

    console.print()
    console.print("[green]Proxy resumed.[/green]")


def _proxy_logs(args: argparse.Namespace):
    """Fetch standalone proxy container logs."""
    render_banner()
    root = find_project_root()
    ctx = _load_proxy_context(root)
    _proxy_require_creds(ctx)

    account = ctx["account"]
    token = ctx["token"]
    names = ctx["names"]
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    warehouse = ctx["warehouse"]
    service_name = names["service"]

    num_lines = getattr(args, "lines", 100)
    instance_id = getattr(args, "instance", "0")

    fqn_service = f"{fqn_schema}.{service_name}"
    sql = (
        f"CALL SYSTEM$GET_SERVICE_LOGS("
        f"'{fqn_service}', '{instance_id}', 'cortex-proxy', {num_lines})"
    )

    console.print(
        f"[bold]Fetching proxy logs:[/bold] {service_name} "
        f"[dim](instance={instance_id}, lines={num_lines})[/dim]"
    )
    console.print()

    try:
        data = snowflake_rest_execute(
            account, token, sql,
            database=db, schema=schema_name, warehouse=warehouse,
        )
        rows = data.get("data", [])
        if rows and rows[0]:
            log_text = rows[0][0]
            if log_text:
                console.print(log_text)
            else:
                console.print("[dim]No log output returned.[/dim]")
        else:
            console.print("[dim]No log output returned.[/dim]")
    except requests.HTTPError as e:
        console.print(f"[red]Failed to fetch logs:[/red] {e}")
        sys.exit(1)


def cmd_proxy(args: argparse.Namespace):
    """Dispatch proxy subcommands."""
    handlers = {
        "setup": _proxy_setup,
        "deploy": _proxy_deploy,
        "status": _proxy_status,
        "suspend": _proxy_suspend,
        "resume": _proxy_resume,
        "logs": _proxy_logs,
    }
    subcmd = getattr(args, "proxy_command", None)
    if not subcmd:
        console.print("[bold]Usage:[/bold] snowclaw proxy <setup|deploy|status|suspend|resume|logs>")
        console.print("[dim]Run [cyan]snowclaw proxy setup[/cyan] to get started.[/dim]")
        return
    handler = handlers.get(subcmd)
    if handler:
        handler(args)
    else:
        console.print(f"[red]Unknown proxy command: {subcmd}[/red]")

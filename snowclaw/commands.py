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
from snowclaw.config import CORTEX_MODELS, provider_for_model, write_connections_toml, write_dotenv, write_openclaw_config
from snowclaw.channels import (
    channel_add,
    channel_edit,
    channel_list,
    channel_remove,
)
from snowclaw.network import (
    ALLOW_ALL_VALUE_LIST,
    CHANNEL_REGISTRY,
    TOOL_REGISTRY,
    NetworkRule,
    NetworkRulesConfig,
    apply_network_rules,
    detect_required_rules,
    diff_rules,
    format_rules_table,
    get_channel_secrets,
    get_env_secrets,
    load_network_config,
    load_network_rules,
    offer_apply_rules,
    parse_host_port,
    print_diff,
    save_network_config,
    save_network_rules,
)
from snowclaw.scaffold import assemble_build_context, assemble_proxy_build_context, scaffold_user_files
from snowclaw.snowflake import (
    apply_runtime_grants,
    build_create_service_grant,
    build_revoke_create_service,
    role_exists,
    run_proxy_snowflake_setup,
    run_snowflake_setup,
    validate_pat_role_restriction,
)
from snowclaw.utils import (
    console,
    find_project_root,
    get_templates_dir,
    load_snowflake_context,
    normalize_openclaw_version,
    read_marker,
    render_banner,
    sf_names,
    sf_proxy_names,
    snowflake_rest_execute,
    write_marker,
)


OPENCLAW_RECOMMENDED_VERSION = "2026.4.15"


def _prompt_openclaw_version(current: str | None = None) -> str:
    """Render the warning panel and run the version select widget.

    Returns the canonical version string (``latest`` or ``YYYY.M.DD``).
    Used by both ``cmd_setup`` and ``cmd_update`` so the UX stays consistent.
    """
    console.print(
        Panel(
            f"[bold]The Docker image is pulled from "
            f"[cyan]ghcr.io/openclaw/openclaw:<version>[/cyan].[/bold]\n\n"
            f"[cyan]{OPENCLAW_RECOMMENDED_VERSION}[/cyan] is the version pinned and "
            "tested against this CLI release — pick it for stability.\n"
            "[cyan]latest[/cyan] tracks upstream and may break unexpectedly between deploys.\n"
            "Pick [cyan]custom[/cyan] only when the upstream team has told you to pin a specific newer build.",
            title="[bold yellow]⚠  OpenClaw version[/bold yellow]",
            border_style="yellow",
            expand=False,
        )
    )

    def _validate(value: str) -> bool:
        try:
            normalize_openclaw_version(value)
            return True
        except ValueError:
            return False

    choice = inquirer.select(
        message="OpenClaw image version:",
        choices=[
            {
                "name": f"{OPENCLAW_RECOMMENDED_VERSION} (Recommended — tested with this CLI release)",
                "value": OPENCLAW_RECOMMENDED_VERSION,
            },
            {
                "name": "latest (tracks upstream — may break between deploys)",
                "value": "latest",
            },
            {"name": "custom (enter your own version)", "value": "custom"},
        ],
    ).execute()

    if choice == "custom":
        default_text = current if current and current not in (OPENCLAW_RECOMMENDED_VERSION, "latest") else ""
        raw = inquirer.text(
            message="Custom OpenClaw version (e.g. 2026.4.15):",
            default=default_text,
            validate=_validate,
            invalid_message="Use 'latest' or CalVer like 2026.4.15 (a leading 'v' is allowed).",
        ).execute()
        return normalize_openclaw_version(raw)

    return choice


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

    console.print()
    console.print(
        Panel(
            "[bold]The admin role provisions SnowClaw — it does NOT run the service.[/bold]\n\n"
            "The CLI uses this role [bold]on your machine[/bold] to create the database, schema,\n"
            "image repo, stage, compute pool, network rule, EAI, and secrets.\n\n"
            "The container runs under a separate [cyan]runtime role[/cyan] (a later prompt), which\n"
            "you create yourself. This admin role never enters the container.\n\n"
            "Needs: [cyan]CREATE DATABASE[/cyan], [cyan]CREATE COMPUTE POOL[/cyan], [cyan]CREATE INTEGRATION[/cyan],\n"
            "[cyan]BIND SERVICE ENDPOINT[/cyan], [cyan]MANAGE GRANTS[/cyan] (all ON ACCOUNT).\n"
            "`ACCOUNTADMIN` has all of these. A dedicated role works too.\n\n"
            "[dim]Full recipe + rationale: https://snowclaw.io/docs/reference/snowflake-privileges[/dim]",
            title="[bold cyan]Admin role[/bold cyan]",
            border_style="cyan",
            expand=False,
        )
    )
    role = inquirer.text(message="Snowflake admin role:", default="SYSADMIN").execute()
    admin_role = role.strip().upper()

    console.print(
        f"\n[dim]The PAT you paste next must be able to assume [cyan]{admin_role}[/cyan].[/dim]"
    )
    pat = inquirer.secret(
        message=f"Programmatic access token (PAT) for {admin_role}:",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="PAT is required.",
    ).execute()
    console.print(
        Panel(
            f"[bold]Your admin PAT should be restricted to[/bold] [cyan]{admin_role}[/cyan].\n"
            "If it isn't, a compromise of the PAT grants whoever holds it every role\n"
            "your user can assume — not just the one intended for SnowClaw.\n\n"
            "Create a restricted PAT with:\n"
            f"  [dim]ALTER USER {sf_user.strip()} ADD PROGRAMMATIC ACCESS TOKEN <name>[/dim]\n"
            f"  [dim]  ROLE_RESTRICTION = '{admin_role}';[/dim]\n\n"
            "[dim]Details: https://snowclaw.io/docs/reference/snowflake-privileges[/dim]",
            title="[bold yellow]⚠  PAT role restriction[/bold yellow]",
            border_style="yellow",
            expand=False,
        )
    )

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

    # --- OpenClaw image version ---
    console.print()
    openclaw_version = _prompt_openclaw_version()

    warehouse = inquirer.text(message="Snowflake warehouse:", default="COMPUTE_WH").execute()

    console.print(
        "\n[dim]SnowClaw service objects (image repo, stage, compute pool, secrets, etc.) "
        "will be created in this database and schema. You can use an existing database/schema "
        f"as long as [cyan]{admin_role}[/cyan] has the required privileges.[/dim]\n"
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

    # --- Runtime role ---
    derived_names = sf_names(database, schema)
    console.print(
        Panel(
            "[bold]The runtime role owns the SPCS service at runtime.[/bold]\n\n"
            "It must already exist and be granted to the admin role above. Create it\n"
            "before continuing (you only need to do this once per Snowflake account):\n\n"
            f"  [dim]USE ROLE USERADMIN;[/dim]\n"
            f"  [dim]CREATE ROLE IF NOT EXISTS SNOWCLAW_RUNTIME_ROLE;[/dim]\n"
            f"  [dim]GRANT ROLE SNOWCLAW_RUNTIME_ROLE TO ROLE {admin_role};[/dim]\n\n"
            "SnowClaw will apply the minimal USAGE/READ grants it needs — you don't\n"
            "have to pre-grant anything else.\n\n"
            "[dim]Details: https://snowclaw.io/docs/reference/snowflake-privileges[/dim]",
            title="[bold cyan]Runtime role[/bold cyan]",
            border_style="cyan",
            expand=False,
        )
    )
    runtime_role = inquirer.text(
        message="Runtime role name:",
        default="SNOWCLAW_RUNTIME_ROLE",
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="Runtime role is required.",
    ).execute().strip().upper()

    try:
        exists = role_exists(account.strip(), pat.strip(), runtime_role, admin_role)
    except requests.HTTPError as e:
        console.print(f"  [yellow]⚠[/yellow] Could not verify role exists: {e}")
        exists = True  # optimistic — don't block setup on a transient error
    if not exists:
        console.print(
            f"  [red]✗[/red] Role [cyan]{runtime_role}[/cyan] does not exist. "
            f"Create it (see panel above), grant it to [cyan]{admin_role}[/cyan], "
            f"then re-run [cyan]snowclaw setup[/cyan]."
        )
        sys.exit(1)

    # --- Runtime-scoped PAT (lives inside the containers) ---
    console.print()
    console.print(
        Panel(
            "[bold]Mint a runtime-scoped PAT[/bold]\n\n"
            "The SPCS containers need a Snowflake token at runtime "
            "(Cortex Code, the Cortex proxy, snowsql all require a PAT — OAuth is rejected).\n"
            "Run this in Snowsight as a user with grant privileges on "
            f"[cyan]{sf_user.strip()}[/cyan]:\n\n"
            f"  [dim]ALTER USER {sf_user.strip()} ADD PROGRAMMATIC ACCESS TOKEN snowclaw_runtime_pat[/dim]\n"
            f"  [dim]  ROLE_RESTRICTION = '{runtime_role}'[/dim]\n"
            f"  [dim]  DAYS_TO_EXPIRY = 90;[/dim]\n\n"
            "Paste the returned token value below. It will be stored as the "
            f"[cyan]{derived_names['secret_sf_token']}[/cyan] Snowflake secret and bound into both containers.",
            title="[bold yellow]⚠  Runtime PAT[/bold yellow]",
            border_style="yellow",
            expand=False,
        )
    )
    runtime_pat = inquirer.secret(
        message=f"Runtime-scoped PAT (ROLE_RESTRICTION = '{runtime_role}'):",
    ).execute().strip()
    if not runtime_pat:
        console.print("[red]Runtime PAT is required — aborting.[/red]")
        sys.exit(1)

    # --- Best-effort PAT role-restriction check ---
    try:
        restricted, available = validate_pat_role_restriction(
            account.strip(), pat.strip(), admin_role
        )
        if not restricted and available:
            console.print(
                f"  [yellow]⚠[/yellow] PAT can assume {len(available)} roles "
                f"({', '.join(available[:3])}{'...' if len(available) > 3 else ''}). "
                f"Consider restricting it to [cyan]{admin_role}[/cyan] only."
            )
    except Exception:
        # Non-fatal — the check is advisory
        pass

    settings = {
        "account": account.strip(),
        "sf_user": sf_user.strip(),
        "pat": pat.strip(),
        "runtime_pat": runtime_pat,
        "channels": channels,
        "warehouse": warehouse.strip(),
        "role": admin_role,              # legacy alias — written to connections.toml
        "admin_role": admin_role,
        "runtime_role": runtime_role,
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
        "sf_user": sf_user.strip(),
        "warehouse": warehouse.strip(),
        "database": database,
        "schema": schema,
        "openclaw_version": openclaw_version,
        "tools": tools,
        "admin_role": admin_role,
        "runtime_role": runtime_role,
        "security_version": 2,
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

    # Offer the allow-all opt-in after the safe-default allowlist flow above.
    console.print()
    skip_allowlist = inquirer.confirm(
        message="Skip the allowlist and permit all outbound traffic instead? (not recommended)",
        default=False,
    ).execute()
    if skip_allowlist:
        from snowclaw.network import print_allow_all_warning

        print_allow_all_warning()
        console.print()
        confirm_allow_all = inquirer.confirm(
            message="Enable allow-all egress?",
            default=False,
        ).execute()
        if confirm_allow_all:
            cfg = load_network_config(root)
            cfg.allow_all_egress = True
            save_network_config(root, cfg)
            console.print("[bold red]Allow-all egress enabled.[/bold red]")

    # --- Optionally create Snowflake objects ---
    console.print()
    create_objects = inquirer.confirm(
        message="Create Snowflake objects now? (database, schema, compute pool, etc.)",
        default=True,
    ).execute()

    cfg = load_network_config(root)

    if create_objects:
        console.print()
        try:
            created_secret_names = run_snowflake_setup(settings)
            console.print()
            console.print("[green]Snowflake objects created successfully.[/green]")

            # Apply approved network rules (or allow-all). The NR/EAI must exist
            # before we can grant USAGE on the EAI to the runtime role.
            if cfg.allow_all_egress or cfg.rules:
                console.print()
                if not apply_network_rules(
                    settings["account"], settings["pat"], names, cfg.rules,
                    allow_all=cfg.allow_all_egress,
                    admin_role=admin_role,
                ):
                    raise RuntimeError("Failed to apply network rules.")

            # Grant minimal USAGE/READ to the runtime role.
            console.print()
            if not apply_runtime_grants(
                settings["account"], settings["pat"], admin_role, runtime_role,
                names, created_secret_names,
            ):
                raise RuntimeError("Failed to apply runtime-role grants.")
        except Exception as e:
            console.print()
            console.print(
                Panel(
                    f"[bold]Snowflake provisioning failed.[/bold]\n\n"
                    f"{e}\n\n"
                    "Fix the underlying issue and re-run [cyan]snowclaw setup[/cyan] "
                    "(use [cyan]--force[/cyan] to overwrite existing template files).",
                    title="[bold red]✗  Setup aborted[/bold red]",
                    border_style="red",
                    expand=False,
                )
            )
            sys.exit(1)

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
    """Create/update Snowflake SECRET objects for enabled channels + tools.

    Secrets are only emitted for channels/tools the user actually enabled at
    setup time. Creating a placeholder secret for disabled tools would leave
    orphan empty-string secrets in the schema (and historically caused
    ``CREATE SERVICE`` to succeed only because the service spec always
    referenced them).

    The ``sf_token`` secret holds the *runtime-scoped* PAT (from
    ``SNOWFLAKE_RUNTIME_TOKEN`` in ``.env``, populated by setup / migration).
    It's bound into both containers so Cortex Code, snowsql, and the Cortex
    proxy can authenticate. The admin PAT (``SNOWFLAKE_TOKEN`` in ``.env``)
    stays on the user's laptop and is never uploaded as a Snowflake secret.
    """
    account = ctx["account"]
    token = ctx["token"]
    marker = ctx.get("marker") or read_marker(root)
    db = names["db"]
    schema_name = names["schema_name"]
    fqn_schema = names["schema"]
    prefix = re.sub(r"_db$", "", db.lower())

    secret_map: dict[str, str] = {}
    runtime_pat = env.get("SNOWFLAKE_RUNTIME_TOKEN", "").strip()
    if runtime_pat:
        secret_map[names["secret_sf_token"]] = runtime_pat
    else:
        console.print(
            "  [yellow]⚠[/yellow] SNOWFLAKE_RUNTIME_TOKEN not set in .env — "
            "skipping sf_token update. Run `snowclaw setup` or edit .env to set it."
        )

    # Tool secrets — only for tools the user enabled.
    enabled_tools: list[str] = marker.get("tools", []) or []
    for tool_name in enabled_tools:
        tool = TOOL_REGISTRY.get(tool_name)
        if not tool:
            continue
        for cred in tool.get("credentials", []):
            if not cred.get("secret"):
                continue
            secret_name = f"{prefix}_{cred['env_var'].lower()}"
            secret_map[secret_name] = env.get(cred["env_var"], "")

    # Channel secrets — dynamically from openclaw.json's enabled channels.
    config_path = root / "openclaw.json"
    if config_path.exists():
        oc_config = json.loads(config_path.read_text())
        enabled_channels = [
            ch for ch, cfg in oc_config.get("channels", {}).items()
            if cfg.get("enabled", False)
        ]
        for sec in get_channel_secrets(prefix, enabled_channels):
            secret_map[sec["secret_name"]] = env.get(sec["env_var"], "")

    # Custom env secrets — anything else in .env that isn't a known config var.
    for sec in get_env_secrets(prefix, root / ".env"):
        secret_map[sec["secret_name"]] = env.get(sec["env_var"], "")

    if not secret_map:
        console.print("  [dim]No secrets to update.[/dim]")
        return

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


def _resolve_roles(ctx: dict) -> tuple[str, str]:
    """Resolve (admin_role, runtime_role) for a deployment.

    Order of precedence:
      1. Marker fields written by setup/upgrade.
      2. ``connections.toml`` role (admin fallback) + default
         ``{prefix}_runtime_role`` (runtime fallback).
      3. Interactive prompt.
    """
    marker = ctx["marker"]
    names = ctx["names"]

    admin_role = (marker.get("admin_role") or "").strip().upper()
    if not admin_role:
        admin_role = (ctx["conn"].get("role") or "").strip().upper()
    if not admin_role:
        admin_role = inquirer.text(
            message="Snowflake admin role:", default="SYSADMIN"
        ).execute().strip().upper()

    runtime_role = (marker.get("runtime_role") or "").strip().upper()
    if not runtime_role:
        runtime_role = names["runtime_role"].upper()
    return admin_role, runtime_role


def _migrate_to_security_v2(
    root: Path, ctx: dict, admin_role: str, default_runtime_role: str
) -> str:
    """Upgrade an existing deployment to the role-separated shape.

    Snowflake blocks ``GRANT OWNERSHIP`` on SPCS services, so we can't
    transfer ownership of an admin-owned service. Instead we drop the old
    service and let the normal ``cmd_deploy`` flow recreate it as the
    runtime role. The stage-mounted volume (skills, workspace, openclaw.json)
    survives ``DROP SERVICE`` — only the service object and its public
    endpoint URL change.

    Steps (idempotent):
      1. Prompt for runtime role name (default = ``{prefix}_runtime_role``).
      2. Create the role if it doesn't exist, grant it to admin.
      3. Apply the minimal USAGE/READ grants (including CORTEX_USER).
      4. Prompt for a runtime-scoped PAT and rotate the ``{prefix}_sf_token``
         secret value (the secret object itself is kept — its previous value
         was the admin PAT; new value is the runtime-scoped PAT).
      5. DROP SERVICE IF EXISTS so it can be recreated as the runtime role.
      6. Persist ``SNOWFLAKE_RUNTIME_TOKEN`` to ``.env`` so subsequent
         ``snowclaw deploy`` runs keep the rotated value in the secret.

    Returns the final runtime role name so ``cmd_deploy`` can use it for
    the subsequent CREATE SERVICE flow.
    """
    from snowclaw.snowflake import build_grant_statements

    account = ctx["account"]
    token = ctx["token"]
    names = ctx["names"]
    fqn_schema = names["schema"]
    service_fqn = f"{fqn_schema}.{names['service']}"

    console.print(
        Panel(
            "[bold]This will upgrade your deployment to the role-separated security model.[/bold]\n\n"
            "• A low-privilege runtime role will own the SPCS service. You must have\n"
            "  already created it and granted it to the admin role (see next prompt).\n"
            f"• The existing service [cyan]{service_fqn}[/cyan] will be [bold]dropped and recreated[/bold]\n"
            "  (Snowflake blocks service ownership transfer).\n"
            "• Your public endpoint URL will change. State on the stage volume\n"
            "  (skills, workspace files, openclaw.json) is preserved.\n"
            "• You will be prompted to mint a new [cyan]runtime-scoped PAT[/cyan]\n"
            "  (the current sf_token holds the admin PAT — we rotate it to a\n"
            "  role-restricted PAT so a compromised container cannot escalate).\n\n"
            "[dim]Expect ~1 minute of service downtime.[/dim]",
            title="[bold yellow]⚠  Security upgrade[/bold yellow]",
            border_style="yellow",
            expand=False,
        )
    )
    if not inquirer.confirm(message="Proceed with upgrade?", default=True).execute():
        console.print("[red]Aborted.[/red]")
        sys.exit(1)

    # --- Runtime role selection ---
    console.print()
    console.print(
        Panel(
            "[bold]The runtime role must already exist and be granted to the admin role.[/bold]\n\n"
            "If it doesn't, create it in Snowsight before continuing:\n\n"
            f"  [dim]USE ROLE USERADMIN;[/dim]\n"
            f"  [dim]CREATE ROLE IF NOT EXISTS {default_runtime_role};[/dim]\n"
            f"  [dim]GRANT ROLE {default_runtime_role} TO ROLE {admin_role};[/dim]",
            title="[bold cyan]Runtime role[/bold cyan]",
            border_style="cyan",
            expand=False,
        )
    )
    runtime_role = inquirer.text(
        message="Runtime role name:",
        default=default_runtime_role,
        validate=lambda v: len(v.strip()) > 0,
        invalid_message="Runtime role is required.",
    ).execute().strip().upper()

    try:
        exists = role_exists(account, token, runtime_role, admin_role)
    except requests.HTTPError as e:
        console.print(f"  [yellow]⚠[/yellow] Could not verify role exists: {e}")
        exists = True
    if not exists:
        console.print(
            f"  [red]✗[/red] Role [cyan]{runtime_role}[/cyan] does not exist. "
            f"Create it (see panel above), grant it to [cyan]{admin_role}[/cyan], "
            f"then re-run [cyan]snowclaw deploy[/cyan]."
        )
        sys.exit(1)

    # Enumerate existing secrets so runtime role gets READ on each.
    try:
        show = snowflake_rest_execute(
            account, token,
            f"SHOW SECRETS IN SCHEMA {fqn_schema}",
            role=admin_role,
        )
        existing_secrets = [
            row[1] for row in (show.get("data") or []) if row and len(row) > 1
        ]
    except requests.HTTPError:
        existing_secrets = []

    console.print()
    console.print("[bold]Applying runtime-role grants...[/bold]")
    for stmt in build_grant_statements(names, runtime_role, existing_secrets):
        try:
            snowflake_rest_execute(account, token, stmt, role=admin_role)
            console.print(f"  [green]✓[/green] {stmt[:72]}")
        except requests.HTTPError as e:
            console.print(f"  [red]✗[/red] {stmt[:72]}")
            console.print(f"    [dim]{e}[/dim]")
            raise

    # --- Rotate sf_token from admin PAT to runtime-scoped PAT ---
    sf_user = ctx.get("user") or ctx["marker"].get("sf_user", "")
    console.print()
    console.print(
        Panel(
            "[bold]Mint a runtime-scoped PAT[/bold]\n\n"
            "The existing [cyan]sf_token[/cyan] secret holds your admin PAT. Rotate it "
            "to a role-restricted PAT so the container can only act as the runtime role:\n\n"
            f"  [dim]ALTER USER {sf_user or '<your_user>'} ADD PROGRAMMATIC ACCESS TOKEN snowclaw_runtime_pat[/dim]\n"
            f"  [dim]  ROLE_RESTRICTION = '{runtime_role}'[/dim]\n"
            f"  [dim]  DAYS_TO_EXPIRY = 90;[/dim]",
            title="[bold yellow]⚠  Runtime PAT[/bold yellow]",
            border_style="yellow",
            expand=False,
        )
    )
    runtime_pat = inquirer.secret(
        message=f"Runtime-scoped PAT (ROLE_RESTRICTION = '{runtime_role}'):",
    ).execute().strip()
    if not runtime_pat:
        console.print("[red]Runtime PAT is required — aborting.[/red]")
        sys.exit(1)

    escaped_pat = runtime_pat.replace("'", "\\'")
    try:
        snowflake_rest_execute(
            account, token,
            f"CREATE OR REPLACE SECRET {fqn_schema}.{names['secret_sf_token']} "
            f"TYPE = GENERIC_STRING SECRET_STRING = '{escaped_pat}'",
            role=admin_role,
        )
        console.print(f"  [green]✓[/green] ROTATE SECRET {names['secret_sf_token']}")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] Failed to rotate sf_token: {e}")
        raise

    # Persist to .env so subsequent `snowclaw deploy` runs keep the same value.
    _persist_runtime_pat_to_env(root, runtime_pat)

    # --- Drop old admin-owned service so it can be recreated as runtime ---
    try:
        show_svc = snowflake_rest_execute(
            account, token,
            f"SHOW SERVICES LIKE '{names['service']}' IN SCHEMA {fqn_schema}",
            role=admin_role,
        )
        service_exists = bool(show_svc.get("data"))
    except requests.HTTPError:
        service_exists = False

    if service_exists:
        console.print()
        console.print("[bold]Dropping existing service...[/bold]")
        try:
            snowflake_rest_execute(
                account, token,
                f"DROP SERVICE IF EXISTS {service_fqn}",
                role=admin_role,
            )
            console.print(f"  [green]✓[/green] DROP SERVICE {service_fqn}")
        except requests.HTTPError as e:
            console.print(f"  [red]✗[/red] DROP SERVICE failed: {e}")
            raise

    return runtime_role


def _persist_runtime_pat_to_env(root: Path, runtime_pat: str) -> None:
    """Write/update SNOWFLAKE_RUNTIME_TOKEN in the project .env file."""
    env_path = root / ".env"
    if not env_path.exists():
        env_path.write_text(f"SNOWFLAKE_RUNTIME_TOKEN={runtime_pat}\n")
        return
    lines = env_path.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith("SNOWFLAKE_RUNTIME_TOKEN="):
            lines[i] = f"SNOWFLAKE_RUNTIME_TOKEN={runtime_pat}"
            found = True
            break
    if not found:
        lines.append(f"SNOWFLAKE_RUNTIME_TOKEN={runtime_pat}")
    env_path.write_text("\n".join(lines) + "\n")


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

    admin_role, runtime_role = _resolve_roles(ctx)
    marker = ctx["marker"]
    security_version = int(marker.get("security_version", 1))
    if security_version < 2:
        runtime_role = _migrate_to_security_v2(root, ctx, admin_role, runtime_role)
        marker["admin_role"] = admin_role
        marker["runtime_role"] = runtime_role
        marker["security_version"] = 2
        # Backfill fields used by scaffold.py when rendering service.yaml env.
        if ctx.get("user") and not marker.get("sf_user"):
            marker["sf_user"] = ctx["user"]
        if ctx.get("warehouse") and not marker.get("warehouse"):
            marker["warehouse"] = ctx["warehouse"]
        write_marker(root, marker)

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
    cfg = load_network_config(root)
    current_rules = cfg.rules

    if cfg.allow_all_egress:
        console.print(
            "  [bold red]Egress mode: ALLOW ALL[/bold red] "
            "[dim](unrestricted — 0.0.0.0:443, 0.0.0.0:80)[/dim]"
        )
        apply_network_rules(account, token, names, current_rules, allow_all=True)
        console.print()
    else:
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

    # Create/alter SPCS service
    console.print()
    console.print("[bold]Creating/updating SPCS service...[/bold]")
    service_spec = (build_dir / "spcs" / "service.yaml").read_text()
    service_name = names["service"]
    pool = names["pool"]
    external_access = names["external_access"]

    # Refresh runtime-role grants (including USAGE on any newly-created
    # secrets from _update_secrets above). Idempotent.
    try:
        show_secrets = snowflake_rest_execute(
            account, token,
            f"SHOW SECRETS IN SCHEMA {fqn_schema}",
            role=admin_role,
        )
        existing_secrets = [
            row[1] for row in (show_secrets.get("data") or []) if row and len(row) > 1
        ]
    except requests.HTTPError:
        existing_secrets = []
    apply_runtime_grants(
        account, token, admin_role, runtime_role, names, existing_secrets,
    )

    # Snowflake blocks GRANT OWNERSHIP on an SPCS service, so the only way
    # for the runtime role to own the service is to create it as that role.
    # We grant CREATE SERVICE on the schema just long enough for the CREATE,
    # then revoke it in the finally block below. Subsequent ALTER SERVICE
    # calls work because runtime holds ownership of the service itself.
    console.print()
    console.print("[bold]Creating/updating SPCS service...[/bold]")
    grant_sql = build_create_service_grant(names, runtime_role)
    revoke_sql = build_revoke_create_service(names, runtime_role)

    try:
        snowflake_rest_execute(account, token, grant_sql, role=admin_role)
        console.print(f"  [green]✓[/green] GRANT CREATE SERVICE → {runtime_role} (transient)")
    except requests.HTTPError as e:
        console.print(f"  [red]✗[/red] GRANT CREATE SERVICE failed: {e}")
        raise

    # REST API doesn't support $$ dollar-quoting; use single-quoted string
    escaped_spec = service_spec.replace("'", "''")
    create_sql = (
        f"CREATE SERVICE IF NOT EXISTS {fqn_schema}.{service_name} "
        f"IN COMPUTE POOL {pool} "
        f"FROM SPECIFICATION '{escaped_spec}' "
        f"EXTERNAL_ACCESS_INTEGRATIONS = ({external_access})"
    )
    alter_spec_sql = (
        f"ALTER SERVICE IF EXISTS {fqn_schema}.{service_name} "
        f"FROM SPECIFICATION '{escaped_spec}'"
    )
    alter_eai_sql = (
        f"ALTER SERVICE IF EXISTS {fqn_schema}.{service_name} "
        f"SET EXTERNAL_ACCESS_INTEGRATIONS = ({external_access})"
    )
    try:
        try:
            snowflake_rest_execute(account, token, create_sql, database=db, schema=schema_name, warehouse=warehouse, role=runtime_role)
            console.print(f"  [green]✓[/green] CREATE SERVICE (as {runtime_role})")
        except requests.HTTPError as e:
            console.print(f"  [red]✗[/red] CREATE SERVICE failed: {e}")
            raise

        try:
            snowflake_rest_execute(account, token, alter_spec_sql, database=db, schema=schema_name, warehouse=warehouse, role=runtime_role)
            console.print(f"  [green]✓[/green] ALTER SERVICE (spec)")
        except requests.HTTPError as e:
            console.print(f"  [red]✗[/red] ALTER SERVICE (spec) failed: {e}")
            raise

        try:
            snowflake_rest_execute(account, token, alter_eai_sql, database=db, schema=schema_name, warehouse=warehouse, role=runtime_role)
            console.print(f"  [green]✓[/green] ALTER SERVICE (external access)")
        except requests.HTTPError as e:
            console.print(f"  [red]✗[/red] ALTER SERVICE (external access) failed: {e}")
            raise
    finally:
        # Always revoke — a failed CREATE still leaves the transient grant in place.
        try:
            snowflake_rest_execute(account, token, revoke_sql, role=admin_role)
            console.print(f"  [green]✓[/green] REVOKE CREATE SERVICE → {runtime_role}")
        except requests.HTTPError as e:
            console.print(
                f"  [yellow]⚠[/yellow] Could not revoke CREATE SERVICE from {runtime_role}: {e}. "
                f"Revoke manually: [dim]{revoke_sql}[/dim]"
            )

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
    console.print()

    new_version = _prompt_openclaw_version(current)

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
    if getattr(args, "skills_only", False):
        return ["skills"]
    if getattr(args, "config_only", False):
        return ["config"]
    return ["skills", "config"]


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
        getattr(args, f, False) for f in ("skills_only", "config_only")
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

    # Always update secrets
    console.print()
    console.print("[bold]Updating Snowflake secrets...[/bold]")
    _update_secrets(root, ctx, names, env)

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
# snowclaw ls / upload / download (workspace-scoped file transfer)
# ---------------------------------------------------------------------------


WORKSPACE_PREFIX = "workspace"


def _open_workspace_connection(root: Path):
    """Open a Snowflake connection for workspace operations.

    Returns (conn, fqn_stage). Exits with an error message if credentials are
    missing.
    """
    from snowclaw.stage import get_sf_connection

    ctx = load_snowflake_context(root)
    account = ctx["account"]
    token = ctx["token"]
    sf_user = ctx["user"]
    names = ctx["names"]

    if not all([account, token, sf_user]):
        console.print("[red]Missing required credentials in .env.[/red]")
        console.print("Required: SNOWFLAKE_ACCOUNT, SNOWFLAKE_TOKEN, SNOWFLAKE_USER")
        sys.exit(1)

    fqn_stage = f"{names['schema']}.{names['stage']}"
    conn = get_sf_connection(
        account=account,
        user=sf_user,
        token=token,
        warehouse=ctx["warehouse"],
        database=names["db"],
        schema=names["schema_name"],
    )
    return conn, fqn_stage


def _normalize_workspace_path(path: str | None) -> str:
    """Strip leading slashes and normalize separators for a workspace-relative path."""
    if not path:
        return ""
    cleaned = path.strip().lstrip("/").rstrip("/")
    return cleaned


def _format_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def cmd_ls(args: argparse.Namespace):
    """List files in the SPCS workspace."""
    from rich.table import Table

    from snowclaw.stage import stage_list

    render_banner()
    root = find_project_root()

    rel = _normalize_workspace_path(getattr(args, "path", None))
    stage_prefix = f"{WORKSPACE_PREFIX}/{rel}" if rel else f"{WORKSPACE_PREFIX}/"

    conn, fqn_stage = _open_workspace_connection(root)
    try:
        files = stage_list(conn, fqn_stage, prefix=stage_prefix)
    finally:
        conn.close()

    display_label = rel if rel else "(root)"
    if not files:
        console.print(f"[dim]No files in workspace/{rel}[/dim]" if rel else "[dim]Workspace is empty.[/dim]")
        return

    table = Table(title=f"workspace/{rel}" if rel else "workspace/", show_header=True, header_style="bold")
    table.add_column("path")
    table.add_column("size", justify="right")
    table.add_column("md5", style="dim")

    workspace_marker = f"/{WORKSPACE_PREFIX}/"
    for f in sorted(files, key=lambda r: r["name"]):
        full_name = f["name"]
        idx = full_name.find(workspace_marker)
        rel_path = full_name[idx + len(workspace_marker):] if idx >= 0 else full_name
        table.add_row(rel_path, _format_size(int(f["size"])), f["md5"] or "")

    console.print(table)
    console.print(f"[dim]{len(files)} file(s) under workspace/{rel}[/dim]" if rel else f"[dim]{len(files)} file(s) in workspace/[/dim]")


def cmd_upload(args: argparse.Namespace):
    """Upload a file to the SPCS workspace (live — agent sees it immediately)."""
    from snowclaw.stage import stage_file_exists, stage_push_file

    render_banner()
    root = find_project_root()

    local_path = Path(args.local_path).expanduser().resolve()
    if not local_path.exists():
        console.print(f"[red]Local file not found:[/red] {local_path}")
        sys.exit(1)
    if local_path.is_dir():
        console.print("[red]Directory uploads are not supported.[/red] Upload individual files (or tar/zip first).")
        sys.exit(1)

    dest_dir = _normalize_workspace_path(getattr(args, "dest", None))
    stage_dir = f"{WORKSPACE_PREFIX}/{dest_dir}" if dest_dir else WORKSPACE_PREFIX
    target_path = f"{stage_dir}/{local_path.name}"

    conn, fqn_stage = _open_workspace_connection(root)
    try:
        if not getattr(args, "force", False):
            if stage_file_exists(conn, fqn_stage, target_path):
                proceed = inquirer.confirm(
                    message=f"workspace/{dest_dir + '/' if dest_dir else ''}{local_path.name} already exists. Overwrite?",
                    default=False,
                ).execute()
                if not proceed:
                    console.print("[yellow]Upload cancelled.[/yellow]")
                    return

        console.print(f"[bold]Uploading {local_path.name} → workspace/{dest_dir + '/' if dest_dir else ''}{local_path.name}...[/bold]")
        stage_push_file(conn, fqn_stage, str(local_path), stage_dir)
    finally:
        conn.close()

    console.print(f"  [green]✓[/green] Uploaded to workspace/{dest_dir + '/' if dest_dir else ''}{local_path.name}")
    console.print("[dim]The workspace volume is live-mounted — the agent sees this file immediately.[/dim]")


def cmd_download(args: argparse.Namespace):
    """Download a file from the SPCS workspace to the local machine."""
    from snowclaw.stage import stage_file_exists, stage_pull_file

    render_banner()
    root = find_project_root()

    rel = _normalize_workspace_path(args.stage_path)
    if not rel:
        console.print("[red]Specify a workspace-relative path to download.[/red]")
        sys.exit(1)
    stage_path = f"{WORKSPACE_PREFIX}/{rel}"

    dest_dir = Path(getattr(args, "dest", None) or ".").expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    conn, fqn_stage = _open_workspace_connection(root)
    try:
        if not stage_file_exists(conn, fqn_stage, stage_path):
            console.print(f"[red]Not found on stage:[/red] workspace/{rel}")
            sys.exit(1)

        console.print(f"[bold]Downloading workspace/{rel} → {dest_dir}/...[/bold]")
        stage_pull_file(conn, fqn_stage, stage_path, str(dest_dir))
    finally:
        conn.close()

    local_file = dest_dir / Path(rel).name
    console.print(f"  [green]✓[/green] Downloaded to {local_file}")


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
        "allow-all": _network_allow_all,
        "restrict": _network_restrict,
    }
    handler = dispatch.get(sub)
    if handler:
        handler(args)


def _print_allow_all_banner():
    console.print(
        "[bold red]Egress mode: ALLOW ALL[/bold red] "
        "[dim](unrestricted — 0.0.0.0:443, 0.0.0.0:80)[/dim]"
    )


def _print_allow_all_warning_note(action: str):
    console.print(
        f"[yellow]Note:[/yellow] allow-all egress is active; this {action} won't take effect "
        "until you run [cyan]snowclaw network restrict[/cyan]."
    )


def _network_list(args: argparse.Namespace):
    """List current approved network rules."""
    render_banner()
    root = find_project_root()
    cfg = load_network_config(root)

    if cfg.allow_all_egress:
        _print_allow_all_banner()
        console.print()
        if cfg.rules:
            console.print(
                "[dim]Saved allowlist (restored on [cyan]snowclaw network restrict[/cyan]):[/dim]"
            )
            console.print(format_rules_table(cfg.rules))
        return

    if not cfg.rules:
        console.print("[dim]No network rules configured.[/dim]")
        console.print(
            "Run [cyan]snowclaw network detect[/cyan] to auto-detect required rules,"
        )
        console.print(
            "or [cyan]snowclaw network add <host>[/cyan] to add one manually."
        )
        return

    console.print(format_rules_table(cfg.rules))
    console.print(f"\n[dim]{len(cfg.rules)} rule(s) total[/dim]")


def _network_add(args: argparse.Namespace):
    """Add a network rule."""
    render_banner()
    root = find_project_root()
    cfg = load_network_config(root)
    rules = cfg.rules

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

    if cfg.allow_all_egress:
        _print_allow_all_warning_note("rule")
        return

    # Offer to apply immediately
    offer_apply_rules(root)


def _network_remove(args: argparse.Namespace):
    """Remove a network rule."""
    render_banner()
    root = find_project_root()
    cfg = load_network_config(root)
    rules = cfg.rules

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

    if cfg.allow_all_egress:
        _print_allow_all_warning_note("removal")
        return

    # Offer to apply immediately
    offer_apply_rules(root)


def _network_apply(args: argparse.Namespace):
    """Apply current network rules to Snowflake."""
    render_banner()
    root = find_project_root()
    cfg = load_network_config(root)

    if cfg.allow_all_egress:
        _print_allow_all_banner()
        console.print()
        approved = inquirer.confirm(
            message="Apply allow-all egress to Snowflake?",
            default=True,
        ).execute()
        if not approved:
            console.print("[dim]Aborted.[/dim]")
            return
    else:
        if not cfg.rules:
            console.print("[yellow]No network rules to apply.[/yellow]")
            console.print("Add rules with [cyan]snowclaw network add <host>[/cyan] first.")
            return

        console.print("[bold]Current network rules:[/bold]")
        console.print(format_rules_table(cfg.rules))
        console.print()

        approved = inquirer.confirm(
            message=f"Apply {len(cfg.rules)} rule(s) to Snowflake?",
            default=True,
        ).execute()

        if not approved:
            console.print("[dim]Aborted.[/dim]")
            return

    ctx = load_snowflake_context(root)
    if not ctx["account"] or not ctx["token"]:
        console.print("[red]Missing Snowflake credentials in .env.[/red]")
        sys.exit(1)

    success = apply_network_rules(
        ctx["account"], ctx["token"], ctx["names"], cfg.rules,
        allow_all=cfg.allow_all_egress,
    )
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
    cfg = load_network_config(root)

    if cfg.allow_all_egress:
        _print_allow_all_banner()
        console.print(
            "[dim]Detection is skipped in allow-all mode. Run "
            "[cyan]snowclaw network restrict[/cyan] to return to the allowlist.[/dim]"
        )
        return

    current = cfg.rules
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


def _network_allow_all(args: argparse.Namespace):
    """Enable allow-all egress mode (all outbound on ports 443 and 80)."""
    from snowclaw.network import print_allow_all_warning

    render_banner()
    root = find_project_root()
    cfg = load_network_config(root)

    if cfg.allow_all_egress:
        _print_allow_all_banner()
        console.print("[dim]Already enabled. Nothing to do.[/dim]")
        return

    print_allow_all_warning()
    console.print()
    confirm_enable = inquirer.confirm(
        message="Enable allow-all egress?",
        default=False,
    ).execute()
    if not confirm_enable:
        console.print("[dim]Aborted. Allowlist mode retained.[/dim]")
        return

    cfg.allow_all_egress = True
    save_network_config(root, cfg)
    console.print("[bold red]Allow-all egress enabled.[/bold red]")

    apply_now = inquirer.confirm(
        message="Apply to Snowflake now?",
        default=True,
    ).execute()
    if not apply_now:
        console.print(
            "[dim]Run [cyan]snowclaw network apply[/cyan] when you're ready.[/dim]"
        )
        return

    ctx = load_snowflake_context(root)
    if not ctx["account"] or not ctx["token"]:
        console.print("[red]Missing Snowflake credentials in .env.[/red]")
        return
    success = apply_network_rules(
        ctx["account"], ctx["token"], ctx["names"], cfg.rules, allow_all=True,
    )
    if success:
        console.print("[green]Applied to Snowflake.[/green]")
    else:
        console.print(
            "[red]Failed to apply. Retry with [cyan]snowclaw network apply[/cyan].[/red]"
        )


def _network_restrict(args: argparse.Namespace):
    """Disable allow-all mode and re-apply the saved allowlist."""
    render_banner()
    root = find_project_root()
    cfg = load_network_config(root)

    if not cfg.allow_all_egress:
        console.print("[dim]Already in allowlist mode. Nothing to do.[/dim]")
        return

    cfg.allow_all_egress = False
    save_network_config(root, cfg)
    console.print("[green]Allowlist mode restored.[/green]")

    if not cfg.rules:
        console.print(
            "[yellow]No rules saved.[/yellow] Run [cyan]snowclaw network detect[/cyan] "
            "to populate the allowlist from your config."
        )
        return

    console.print("[bold]Saved rules:[/bold]")
    console.print(format_rules_table(cfg.rules))
    console.print()

    apply_now = inquirer.confirm(
        message=f"Apply {len(cfg.rules)} rule(s) to Snowflake now?",
        default=True,
    ).execute()
    if not apply_now:
        console.print(
            "[dim]Run [cyan]snowclaw network apply[/cyan] when you're ready.[/dim]"
        )
        return

    ctx = load_snowflake_context(root)
    if not ctx["account"] or not ctx["token"]:
        console.print("[red]Missing Snowflake credentials in .env.[/red]")
        return
    success = apply_network_rules(
        ctx["account"], ctx["token"], ctx["names"], cfg.rules, allow_all=False,
    )
    if success:
        console.print("[green]Applied to Snowflake.[/green]")
    else:
        console.print(
            "[red]Failed to apply. Retry with [cyan]snowclaw network apply[/cyan].[/red]"
        )


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


def cmd_plugins(args: argparse.Namespace):
    """Manage OpenClaw plugins."""
    from snowclaw.plugins import plugins_add, plugins_list, plugins_remove

    sub = getattr(args, "plugins_command", None)
    root = find_project_root()
    if not sub:
        plugins_list(root)
        return

    dispatch = {
        "list": lambda a: plugins_list(root),
        "add": lambda a: plugins_add(root, a.spec),
        "remove": lambda a: plugins_remove(root, a.id),
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
        prefixed = f"{provider_for_model(m['id'])}/{m['id']}"
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

    # Strip whichever provider prefix the current model uses so the picker default lines up.
    current_id = current
    for prefix in ("cortex-claude/", "cortex-openai/", "cortex/"):
        if current.startswith(prefix):
            current_id = current[len(prefix):]
            break

    selected = inquirer.select(
        message="Select default model:",
        choices=[
            {"name": m["name"], "value": m["id"]}
            for m in CORTEX_MODELS
        ],
        default=current_id or None,
    ).execute()

    config.setdefault("agents", {}).setdefault("defaults", {})["model"] = f"{provider_for_model(selected)}/{selected}"
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
    follow = getattr(args, "tail", False)
    interval = max(0.5, float(getattr(args, "interval", 2.0)))

    fqn_service = f"{fqn_schema}.{service_name}"

    def _fetch(lines: int) -> str:
        sql = (
            f"CALL SYSTEM$GET_SERVICE_LOGS("
            f"'{fqn_service}', '{instance_id}', '{container}', {lines})"
        )
        data = snowflake_rest_execute(
            account, token, sql,
            database=db, schema=schema_name, warehouse=warehouse,
        )
        rows = data.get("data", [])
        if rows and rows[0] and rows[0][0]:
            return rows[0][0]
        return ""

    header_extra = f", follow=True, interval={interval}s" if follow else ""
    console.print(
        f"[bold]Fetching logs:[/bold] {service_name} "
        f"[dim](container={container}, instance={instance_id}, lines={num_lines}{header_extra})[/dim]"
    )
    console.print()

    if not follow:
        try:
            log_text = _fetch(num_lines)
            if log_text:
                console.print(log_text)
            else:
                console.print("[dim]No log output returned.[/dim]")
        except requests.HTTPError as e:
            console.print(f"[red]Failed to fetch logs:[/red] {e}")
            sys.exit(1)
        return

    # Follow mode: poll SYSTEM$GET_SERVICE_LOGS, dedupe against the last printed
    # line. Snowflake returns the most recent N lines per call, so if log volume
    # exceeds `poll_lines` per `interval` we'll miss output — bump --lines or
    # shrink --interval if that warning fires.
    poll_lines = max(num_lines, 200)
    last_line: str | None = None
    console.print("[dim]Tailing logs (Ctrl+C to stop)...[/dim]")
    try:
        while True:
            try:
                log_text = _fetch(poll_lines)
            except requests.HTTPError as e:
                console.print(f"[yellow]Transient fetch error:[/yellow] {e}")
                time.sleep(interval)
                continue

            if log_text:
                lines = log_text.splitlines()
                if last_line is None:
                    new_lines = lines[-num_lines:]
                else:
                    idx = None
                    for i in range(len(lines) - 1, -1, -1):
                        if lines[i] == last_line:
                            idx = i
                            break
                    if idx is None:
                        console.print(
                            "[yellow]Log buffer rolled past last seen line; "
                            "some lines may have been missed. "
                            "Consider raising --lines or lowering --interval.[/yellow]"
                        )
                        new_lines = lines
                    else:
                        new_lines = lines[idx + 1:]
                for line in new_lines:
                    console.print(line)
                if new_lines:
                    last_line = new_lines[-1]

            time.sleep(interval)
    except KeyboardInterrupt:
        console.print()
        console.print("[dim]Stopped tailing.[/dim]")


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
        except Exception as e:
            console.print()
            console.print(
                Panel(
                    f"[bold]Snowflake provisioning failed.[/bold]\n\n"
                    f"{e}\n\n"
                    "Fix the underlying issue and re-run [cyan]snowclaw proxy setup[/cyan].",
                    title="[bold red]✗  Setup aborted[/bold red]",
                    border_style="red",
                    expand=False,
                )
            )
            sys.exit(1)

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
    base_url_root = endpoint_url if endpoint_url else "https://<proxy-endpoint>"
    base_url_v1 = f"{base_url_root}/v1"
    console.print()
    console.print(Panel(
        '[bold]Add this to your openclaw.json to connect through the proxy:[/bold]\n\n'
        '[cyan]{\n'
        '  "models": {\n'
        '    "providers": {\n'
        '      "cortex-claude": {\n'
        f'        "baseUrl": "{base_url_root}",\n'
        '        "apiKey": "$SNOWFLAKE_TOKEN",\n'
        '        "headers": {\n'
        '          "X-Cortex-Token": "$SNOWFLAKE_TOKEN"\n'
        '        },\n'
        '        "api": "anthropic-messages"\n'
        '      },\n'
        '      "cortex-openai": {\n'
        f'        "baseUrl": "{base_url_v1}",\n'
        '        "apiKey": "$SNOWFLAKE_TOKEN",\n'
        '        "headers": {\n'
        '          "X-Cortex-Token": "$SNOWFLAKE_TOKEN"\n'
        '        },\n'
        '        "api": "openai-completions"\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}[/cyan]\n\n'
        '[dim]Use cortex-claude for Claude models (native caching via /v1/messages).\n'
        'Use cortex-openai for OpenAI, Snowflake, and Llama models (/v1/chat/completions).\n'
        'Each user authenticates with their own PAT — apiKey handles SPCS ingress auth,\n'
        'while X-Cortex-Token is passed through to Cortex for the actual LLM request.[/dim]',
        title="OpenClaw Provider Config",
        border_style="green",
        expand=False,
    ))
    if not endpoint_url:
        console.print(
            "\n[dim]Endpoint URL is still provisioning."
            " Run [cyan]snowclaw proxy status[/cyan] to get it once ready.[/dim]"
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

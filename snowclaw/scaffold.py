"""Scaffolding user files and assembling build context."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from snowclaw.config import migrate_openclaw_config
from snowclaw.network import (
    CHANNEL_REGISTRY,
    TOOL_REGISTRY,
    build_network_rule_sql,
    get_channel_secrets,
    get_env_secrets,
    load_network_rules,
)
from snowclaw.utils import console, get_templates_dir, read_marker, sf_names, sf_proxy_names


DOCKER_COMPOSE_TEMPLATE = """\
services:
  cortex-proxy:
    build: ./proxy
    network_mode: host
    env_file: ../../.env
    restart: unless-stopped

  openclaw:
    build: .
    network_mode: host
    env_file: ../../.env
    depends_on:
      - cortex-proxy
    volumes:
      - openclaw-data:/home/node/.openclaw
      - ../../openclaw.json:/home/node/.openclaw/openclaw.json:ro
    restart: unless-stopped

volumes:
  openclaw-data:
"""


def scaffold_user_files(target: Path, force: bool = False) -> tuple[list[str], list[str]]:
    """Scaffold only user-editable files into the project directory.

    Copies: skills/, .gitignore. Creates: workspace/.
    Does NOT copy Dockerfile, docker-compose.yml, scripts/, spcs/, plugins/.
    """
    templates = get_templates_dir()
    if not templates.is_dir():
        console.print(f"[red]Templates directory not found at {templates}[/red]")
        sys.exit(1)

    copied: list[str] = []
    skipped: list[str] = []

    # Copy skills/
    skills_src = templates / "skills"
    if skills_src.is_dir():
        for src in sorted(skills_src.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(templates)
            dest = target / rel
            if dest.exists() and not force:
                skipped.append(str(rel))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(str(rel))

    # Copy .gitignore
    gitignore_src = templates / ".gitignore"
    if gitignore_src.exists():
        dest = target / ".gitignore"
        if dest.exists() and not force:
            skipped.append(".gitignore")
        else:
            shutil.copy2(gitignore_src, dest)
            copied.append(".gitignore")

    # Create workspace/ directory
    workspace = target / "workspace"
    if not workspace.exists():
        workspace.mkdir()
        (workspace / ".gitkeep").touch()
        copied.append("workspace/")

    # Create build-hooks/ directory with README
    build_hooks = target / "build-hooks"
    if not build_hooks.exists():
        build_hooks.mkdir()
        (build_hooks / ".gitkeep").touch()
        (build_hooks / "README.md").write_text(
            "# Build Hooks\n"
            "\n"
            "Place .sh scripts here to customize your Docker image.\n"
            "Scripts run alphabetically during `snowclaw build` as root.\n"
            "\n"
            "Examples:\n"
            "  00-install-ffmpeg.sh:  apt-get update && apt-get install -y ffmpeg\n"
            "  01-install-python.sh:  pip install pandas numpy\n"
            "\n"
            "Note: These run at build time, not runtime. Environment variables\n"
            "and secrets are NOT available. Use for package installs and static config.\n"
        )
        copied.append("build-hooks/")

    return copied, skipped


def assemble_build_context(root: Path) -> Path:
    """Generate .snowclaw/build/ from user config + CLI templates.

    Blows away and regenerates the build directory each time.
    Returns the path to the build directory.
    """
    marker = read_marker(root)
    database = marker.get("database", "snowclaw_db")
    schema_name = marker.get("schema", "snowclaw_schema")
    prefix = re.sub(r"_db$", "", database.lower())
    openclaw_version = marker.get("openclaw_version", "latest")
    templates = get_templates_dir()

    # Auto-migrate pre-existing openclaw.json (single `cortex` provider) to the
    # cortex-claude/cortex-openai split. No-op if already migrated.
    migrate_openclaw_config(root)

    build_dir = root / ".snowclaw" / "build"

    # Clean and recreate
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    # Copy Dockerfile from templates, substitute version
    dockerfile_src = templates / "Dockerfile"
    dockerfile_content = dockerfile_src.read_text()
    dockerfile_content = re.sub(
        r"ARG OPENCLAW_VERSION=\S+",
        f"ARG OPENCLAW_VERSION={openclaw_version}",
        dockerfile_content,
    )

    # Inject build hooks layer if user has .sh scripts in build-hooks/
    build_hooks_src = root / "build-hooks"
    has_hooks = (
        build_hooks_src.is_dir()
        and any(build_hooks_src.glob("*.sh"))
    )
    if has_hooks:
        shutil.copytree(
            build_hooks_src,
            build_dir / "build-hooks",
            ignore=shutil.ignore_patterns(".gitkeep", "README.md"),
        )
        hook_layer = (
            "\n# User build hooks\n"
            "COPY build-hooks/ /tmp/build-hooks/\n"
            'RUN for script in /tmp/build-hooks/*.sh; do [ -f "$script" ]'
            ' && chmod +x "$script" && echo "Running $script..."'
            ' && "$script"; done && rm -rf /tmp/build-hooks\n'
        )
        # Insert after the GitHub CLI install block, before mkdir -p /home/node/.openclaw
        dockerfile_content = dockerfile_content.replace(
            "# Ensure the openclaw home dir",
            hook_layer + "\n# Ensure the openclaw home dir",
        )

    (build_dir / "Dockerfile").write_text(dockerfile_content)

    # Generate docker-compose.yml
    (build_dir / "docker-compose.yml").write_text(DOCKER_COMPOSE_TEMPLATE)

    # Copy docker-entrypoint.sh into scripts/
    scripts_dir = build_dir / "scripts"
    scripts_dir.mkdir()
    entrypoint_src = templates / "scripts" / "docker-entrypoint.sh"
    shutil.copy2(entrypoint_src, scripts_dir / "docker-entrypoint.sh")

    # openclaw.json and connections.toml are NOT copied into the build context —
    # they're uploaded directly to the stage by deploy/push instead of being
    # baked into the image.

    # Copy user skills/
    skills_src = root / "skills"
    if skills_src.is_dir():
        shutil.copytree(skills_src, build_dir / "skills")

    # Create empty workspace/ (not baked from user dir — managed via push/pull)
    (build_dir / "workspace").mkdir()

    # Copy plugins from CLI templates
    plugins_src = templates / "plugins"
    if plugins_src.is_dir():
        shutil.copytree(plugins_src, build_dir / "plugins")

    # Copy proxy/ from CLI repo into build context
    cli_root = templates.parent
    proxy_src = cli_root / "proxy"
    if proxy_src.is_dir():
        shutil.copytree(proxy_src, build_dir / "proxy")

    # Copy and prefix-substitute SPCS files
    spcs_dir = build_dir / "spcs"
    spcs_dir.mkdir()

    # Determine enabled channels from openclaw.json for service.yaml secrets
    enabled_channels: list[str] = []
    openclaw_json = root / "openclaw.json"
    if openclaw_json.exists():
        oc_config = json.loads(openclaw_json.read_text())
        enabled_channels = [
            ch for ch, cfg in oc_config.get("channels", {}).items()
            if cfg.get("enabled", False)
        ]

    # Build channel secrets YAML block for service.yaml
    fqn_schema = f"{database}.{schema_name}"
    channel_secrets_yaml = ""
    for sec in get_channel_secrets(prefix, enabled_channels):
        channel_secrets_yaml += (
            f"        - snowflakeSecret: {fqn_schema}.{sec['secret_name']}\n"
            f"          secretKeyRef: secret_string\n"
            f"          envVarName: {sec['env_var']}\n"
        )

    # Build env secrets YAML block from all qualifying .env vars
    env_file = root / ".env"
    env_secrets = get_env_secrets(prefix, env_file)
    env_secrets_yaml = ""
    for sec in env_secrets:
        env_secrets_yaml += (
            f"        - snowflakeSecret: {fqn_schema}.{sec['secret_name']}\n"
            f"          secretKeyRef: secret_string\n"
            f"          envVarName: {sec['env_var']}\n"
        )

    # Build SNOWCLAW_MASK_VARS from secret credentials that are configured
    # (reads .env to check which vars actually have values)
    mask_var_names = ["SNOWFLAKE_TOKEN"]
    for registry in (CHANNEL_REGISTRY, TOOL_REGISTRY):
        for entry in registry.values():
            for cred in entry.get("credentials", []):
                if cred.get("secret") and cred["env_var"] not in mask_var_names:
                    mask_var_names.append(cred["env_var"])

    # Filter to only vars that have values in .env
    env_values: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env_values[k.strip()] = v.strip()
    mask_vars_list = [v for v in mask_var_names if env_values.get(v)]
    # Add env secret var names to mask list
    for sec in env_secrets:
        if sec["env_var"] not in mask_vars_list:
            mask_vars_list.append(sec["env_var"])
    mask_vars_value = ",".join(mask_vars_list)

    account = marker.get("account", "")

    for name in ("service.yaml", "image-repo.sql"):
        src = templates / "spcs" / name
        if src.exists():
            content = src.read_text()
            content = content.replace("__SNOWCLAW_DB__", database)
            content = content.replace("__SNOWCLAW_SCHEMA__", schema_name)
            content = content.replace("__SNOWCLAW_PREFIX__", prefix)
            if name == "service.yaml":
                content = content.replace("__CHANNEL_SECRETS__", channel_secrets_yaml)
                content = content.replace("__CHANNEL_SECRETS_PROXY__", channel_secrets_yaml)
                content = content.replace("__ENV_SECRETS__", env_secrets_yaml)
                content = content.replace("__ENV_SECRETS_PROXY__", env_secrets_yaml)
                content = content.replace("__SNOWCLAW_MASK_VARS__", mask_vars_value)
                content = content.replace("__SNOWCLAW_ACCOUNT__", account)
            (spcs_dir / name).write_text(content)

    # Generate network-rules.sql from saved rules
    names = sf_names(database, schema_name)
    rules = load_network_rules(root)
    if rules:
        stmts = build_network_rule_sql(names, rules)
        if stmts:
            header = (
                "-- Network rules (generated from .snowclaw/network-rules.json)\n"
                "-- Manage with: snowclaw network list|add|remove|apply|detect\n\n"
            )
            (spcs_dir / "network-rules.sql").write_text(
                header + ";\n\n".join(stmts) + ";\n"
            )

    return build_dir


def assemble_proxy_build_context(root: Path) -> Path:
    """Generate .snowclaw/build-proxy/ for standalone proxy deployment.

    Only includes the proxy source and the proxy-specific service.yaml.
    Returns the path to the build directory.
    """
    marker = read_marker(root)
    database = marker.get("database", "snowclaw_db")
    schema_name = marker.get("schema", "snowclaw_schema")
    account = marker.get("account", "")
    prefix = re.sub(r"_db$", "", database.lower())
    templates = get_templates_dir()

    build_dir = root / ".snowclaw" / "build-proxy"

    # Clean and recreate
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    # Copy proxy/ from CLI repo
    cli_root = templates.parent
    proxy_src = cli_root / "proxy"
    if proxy_src.is_dir():
        shutil.copytree(proxy_src, build_dir / "proxy")

    # Copy and substitute proxy-service.yaml
    spcs_dir = build_dir / "spcs"
    spcs_dir.mkdir()

    svc_src = templates / "spcs" / "proxy-service.yaml"
    content = svc_src.read_text()
    content = content.replace("__SNOWCLAW_DB__", database)
    content = content.replace("__SNOWCLAW_SCHEMA__", schema_name)
    content = content.replace("__SNOWCLAW_PREFIX__", prefix)
    content = content.replace("__SNOWCLAW_ACCOUNT__", account)
    (spcs_dir / "proxy-service.yaml").write_text(content)

    return build_dir

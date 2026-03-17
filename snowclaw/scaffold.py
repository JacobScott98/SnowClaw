"""Scaffolding user files and assembling build context."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from snowclaw.utils import console, get_templates_dir, read_marker


DOCKER_COMPOSE_TEMPLATE = """\
services:
  openclaw:
    build: .
    network_mode: host
    env_file: ../../.env
    volumes:
      - openclaw-data:/home/node/.openclaw
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
    (build_dir / "Dockerfile").write_text(dockerfile_content)

    # Generate docker-compose.yml
    (build_dir / "docker-compose.yml").write_text(DOCKER_COMPOSE_TEMPLATE)

    # Copy docker-entrypoint.sh into scripts/
    scripts_dir = build_dir / "scripts"
    scripts_dir.mkdir()
    entrypoint_src = templates / "scripts" / "docker-entrypoint.sh"
    shutil.copy2(entrypoint_src, scripts_dir / "docker-entrypoint.sh")

    # Copy user config into config/
    config_dir = build_dir / "config"
    config_dir.mkdir()

    openclaw_json = root / "openclaw.json"
    if openclaw_json.exists():
        shutil.copy2(openclaw_json, config_dir / "openclaw.json")

    connections_toml = root / "connections.toml"
    if connections_toml.exists():
        shutil.copy2(connections_toml, config_dir / "connections.toml")

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

    # Copy and prefix-substitute SPCS files
    spcs_dir = build_dir / "spcs"
    spcs_dir.mkdir()
    for name in ("service.yaml", "image-repo.sql"):
        src = templates / "spcs" / name
        if src.exists():
            content = src.read_text()
            content = content.replace("__SNOWCLAW_DB__", database)
            content = content.replace("__SNOWCLAW_SCHEMA__", schema_name)
            content = content.replace("__SNOWCLAW_PREFIX__", prefix)
            (spcs_dir / name).write_text(content)

    return build_dir

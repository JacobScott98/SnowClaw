"""Plugin management for OpenClaw."""

from __future__ import annotations

import json
from pathlib import Path

from rich.table import Table

from snowclaw.utils import console


def load_plugins(root: Path) -> list[dict]:
    """Read .snowclaw/plugins.json and return the plugin list."""
    plugins_file = root / ".snowclaw" / "plugins.json"
    if not plugins_file.exists():
        return []
    data = json.loads(plugins_file.read_text())
    return data.get("plugins", [])


def save_plugins(root: Path, plugins: list[dict]):
    """Write the plugin list to .snowclaw/plugins.json."""
    plugins_file = root / ".snowclaw" / "plugins.json"
    plugins_file.parent.mkdir(parents=True, exist_ok=True)
    plugins_file.write_text(json.dumps({"plugins": plugins}, indent=2) + "\n")


def _derive_id(spec: str) -> str:
    """Derive a plugin id from an npm spec or path.

    @openclaw/voice-call          -> voice-call
    @memtensor/memos-cloud-plugin -> memos-cloud-plugin
    voice-call                    -> voice-call
    ./my-plugin                   -> my-plugin
    /abs/path/to/my-plugin        -> my-plugin
    """
    # Path-based: use the directory basename
    if spec.startswith(".") or spec.startswith("/"):
        return Path(spec).name

    # npm scoped: strip @scope/
    if "/" in spec:
        return spec.split("/", 1)[1]

    return spec


def _is_path_spec(spec: str) -> bool:
    """Determine if a spec refers to a local path."""
    return spec.startswith(".") or spec.startswith("/")


def plugins_list(root: Path):
    """Display configured plugins."""
    plugins = load_plugins(root)

    if not plugins:
        console.print("  No plugins configured.")
        console.print("  Use [bold]snowclaw plugins add <spec>[/bold] to add one.")
        return

    table = Table(show_header=True, header_style="bold", padding=(0, 2))
    table.add_column("Plugin")
    table.add_column("Source")
    table.add_column("Spec")

    for p in plugins:
        source = p["source"]
        spec = p.get("package", "") if source == "npm" else p.get("path", "")
        table.add_row(p["id"], source, spec)

    console.print(table)


def plugins_add(root: Path, spec: str):
    """Add a plugin by npm spec or local path."""
    plugins = load_plugins(root)
    plugin_id = _derive_id(spec)

    # Check for duplicates
    if any(p["id"] == plugin_id for p in plugins):
        console.print(f"  [yellow]Plugin '{plugin_id}' is already configured.[/yellow]")
        return

    if _is_path_spec(spec):
        # Path-based plugin
        plugin_path = Path(spec)
        if not plugin_path.is_absolute():
            plugin_path = root / plugin_path
        if not plugin_path.is_dir():
            console.print(f"  [red]Directory not found:[/red] {spec}")
            return
        # Store relative to project root
        try:
            rel_path = str(plugin_path.relative_to(root))
        except ValueError:
            rel_path = str(plugin_path)
        plugins.append({
            "id": plugin_id,
            "source": "path",
            "path": rel_path,
        })
    else:
        # npm package
        plugins.append({
            "id": plugin_id,
            "source": "npm",
            "package": spec,
        })

    save_plugins(root, plugins)
    console.print(f"  [green]\u2713[/green] Added plugin '{plugin_id}' ({spec})")
    console.print("  Run [bold]snowclaw build[/bold] or [bold]snowclaw deploy[/bold] to apply.")


def plugins_remove(root: Path, plugin_id: str):
    """Remove a plugin by id."""
    plugins = load_plugins(root)
    original_count = len(plugins)
    plugins = [p for p in plugins if p["id"] != plugin_id]

    if len(plugins) == original_count:
        console.print(f"  [yellow]Plugin '{plugin_id}' not found.[/yellow]")
        return

    save_plugins(root, plugins)
    console.print(f"  [green]\u2713[/green] Removed plugin '{plugin_id}'")
    console.print("  Run [bold]snowclaw build[/bold] or [bold]snowclaw deploy[/bold] to apply.")

"""CLI entry point and argument parser."""

from __future__ import annotations

import argparse

from snowclaw import __version__
from snowclaw.commands import (
    cmd_build,
    cmd_channel,
    cmd_deploy,
    cmd_dev,
    cmd_download,
    cmd_logs,
    cmd_ls,
    cmd_model,
    cmd_network,
    cmd_plugins,
    cmd_proxy,
    cmd_pull,
    cmd_push,
    cmd_restart,
    cmd_resume,
    cmd_setup,
    cmd_status,
    cmd_suspend,
    cmd_update,
    cmd_upgrade,
    cmd_upload,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snowclaw",
        description="SnowClaw — OpenClaw on Snowflake Container Services",
    )
    parser.add_argument("--version", action="version", version=f"snowclaw {__version__}")

    sub = parser.add_subparsers(dest="command")
    setup_parser = sub.add_parser("setup", help="Interactive first-time setup wizard")
    setup_parser.add_argument("--force", action="store_true", help="Overwrite existing template files")
    sub.add_parser("dev", help="Assemble build context and run locally with docker compose")
    build_parser = sub.add_parser("build", help="Assemble build context and build Docker image")
    build_parser.add_argument("--tag", default="latest", help="Docker image tag (default: latest)")
    sub.add_parser("deploy", help="Build, push, and deploy to SPCS")
    sub.add_parser("status", help="Show deployed service status, endpoints, and compute pool")
    sub.add_parser("suspend", help="Suspend the SPCS service and compute pool")
    sub.add_parser("resume", help="Resume the SPCS compute pool and service")
    sub.add_parser("restart", help="Restart the SPCS service to pick up config changes")
    sub.add_parser("update", help="Update the OpenClaw version")
    sub.add_parser("upgrade", help="Update SnowClaw CLI to the latest version")

    logs_parser = sub.add_parser("logs", help="Show container logs from the SPCS service")
    logs_parser.add_argument("-n", "--lines", type=int, default=100, help="Number of log lines (default: 100)")
    logs_parser.add_argument("-p", "--proxy", action="store_true", help="Show proxy container logs instead of openclaw")
    logs_parser.add_argument("--container", default="openclaw", help="Container name (default: openclaw)")
    logs_parser.add_argument("--instance", default="0", help="Instance ID (default: 0)")

    pull_parser = sub.add_parser(
        "pull",
        help="Pull skills and config from SPCS stage (workspace files: use `snowclaw download`)",
    )
    pull_group = pull_parser.add_mutually_exclusive_group()
    pull_group.add_argument("--skills-only", action="store_true", help="Only pull skills/")
    pull_group.add_argument("--config-only", action="store_true", help="Only pull openclaw.json")

    push_parser = sub.add_parser(
        "push",
        help="Push skills and config to SPCS stage (workspace files: use `snowclaw upload`)",
    )
    push_group = push_parser.add_mutually_exclusive_group()
    push_group.add_argument("--skills-only", action="store_true", help="Only push skills/")
    push_group.add_argument("--config-only", action="store_true", help="Only push openclaw.json")
    push_parser.add_argument("--secrets", action="store_true", help="Only update secrets and connections.toml (skip target push when used alone)")

    # --- snowclaw ls / upload / download (workspace file transfer) ---
    ls_parser = sub.add_parser(
        "ls", help="List files in the SPCS workspace (paths are workspace-relative)"
    )
    ls_parser.add_argument("path", nargs="?", default="", help="Subpath under workspace/ to list (default: workspace root)")

    upload_parser = sub.add_parser(
        "upload", help="Upload a local file into the SPCS workspace (live — agent sees it immediately)"
    )
    upload_parser.add_argument("local_path", help="Path to a local file to upload")
    upload_parser.add_argument("--dest", default="", help="Destination subdirectory under workspace/ (default: workspace root). Filename is preserved.")
    upload_parser.add_argument("--force", action="store_true", help="Overwrite without confirmation if destination already exists")

    download_parser = sub.add_parser(
        "download", help="Download a file from the SPCS workspace to the local machine"
    )
    download_parser.add_argument("stage_path", help="Workspace-relative path to download (e.g. report.csv or data/report.csv)")
    download_parser.add_argument("--dest", default=".", help="Local destination directory (default: cwd). Filename is preserved.")

    # --- snowclaw network ---
    net_parser = sub.add_parser(
        "network", help="Manage network rules for SPCS external access"
    )
    net_sub = net_parser.add_subparsers(dest="network_command")

    net_sub.add_parser("list", help="List current approved network rules")

    add_parser = net_sub.add_parser("add", help="Add a network rule")
    add_parser.add_argument("host", help="Host or host:port (default port 443)")
    add_parser.add_argument("--reason", "-r", default="", help="Reason for this rule")

    remove_parser = net_sub.add_parser("remove", help="Remove a network rule")
    remove_parser.add_argument("host", help="Host or host:port to remove")

    net_sub.add_parser("apply", help="Apply current rules to Snowflake")
    net_sub.add_parser("detect", help="Auto-detect required rules from project config")
    net_sub.add_parser(
        "allow-all",
        help="Permit all outbound traffic (0.0.0.0:443, 0.0.0.0:80) — NOT RECOMMENDED",
    )
    net_sub.add_parser(
        "restrict",
        help="Disable allow-all mode and re-apply the saved allowlist",
    )

    # --- snowclaw channel ---
    ch_parser = sub.add_parser(
        "channel", help="Manage communication channel configurations"
    )
    ch_sub = ch_parser.add_subparsers(dest="channel_command")

    ch_sub.add_parser("list", help="List configured channels")
    ch_sub.add_parser("add", help="Interactive wizard to add a channel")

    ch_remove_parser = ch_sub.add_parser("remove", help="Remove a channel")
    ch_remove_parser.add_argument("name", help="Channel type to remove (e.g. slack, telegram, discord)")

    ch_edit_parser = ch_sub.add_parser("edit", help="Edit channel credentials")
    ch_edit_parser.add_argument("name", help="Channel type to edit (e.g. slack, telegram, discord)")

    # --- snowclaw plugins ---
    plugins_parser = sub.add_parser("plugins", help="Manage OpenClaw plugins")
    plugins_sub = plugins_parser.add_subparsers(dest="plugins_command")

    plugins_sub.add_parser("list", help="List configured plugins")

    plugins_add_parser = plugins_sub.add_parser("add", help="Add a plugin")
    plugins_add_parser.add_argument("spec", help="npm package (e.g. @openclaw/voice-call) or local path")

    plugins_remove_parser = plugins_sub.add_parser("remove", help="Remove a plugin")
    plugins_remove_parser.add_argument("id", help="Plugin id to remove")

    # --- snowclaw model ---
    model_parser = sub.add_parser("model", help="View or change the default agent model")
    model_sub = model_parser.add_subparsers(dest="model_command")
    model_sub.add_parser("list", help="List available models")
    model_sub.add_parser("set", help="Change the default model")

    # --- snowclaw proxy ---
    proxy_parser = sub.add_parser(
        "proxy", help="Deploy a standalone Cortex proxy for external OpenClaw agents"
    )
    proxy_sub = proxy_parser.add_subparsers(dest="proxy_command")

    proxy_sub.add_parser("setup", help="Interactive setup wizard for standalone proxy")
    proxy_sub.add_parser("deploy", help="Build, push, and deploy standalone proxy to SPCS")
    proxy_sub.add_parser("status", help="Show standalone proxy service status and endpoint")
    proxy_sub.add_parser("suspend", help="Suspend the standalone proxy service and compute pool")
    proxy_sub.add_parser("resume", help="Resume the standalone proxy compute pool and service")

    proxy_logs_parser = proxy_sub.add_parser("logs", help="Show standalone proxy container logs")
    proxy_logs_parser.add_argument("-n", "--lines", type=int, default=100, help="Number of log lines (default: 100)")
    proxy_logs_parser.add_argument("--instance", default="0", help="Instance ID (default: 0)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "setup": cmd_setup,
        "dev": cmd_dev,
        "build": cmd_build,
        "deploy": cmd_deploy,
        "status": cmd_status,
        "suspend": cmd_suspend,
        "resume": cmd_resume,
        "restart": cmd_restart,
        "update": cmd_update,
        "pull": cmd_pull,
        "push": cmd_push,
        "ls": cmd_ls,
        "upload": cmd_upload,
        "download": cmd_download,
        "network": cmd_network,
        "channel": cmd_channel,
        "plugins": cmd_plugins,
        "model": cmd_model,
        "proxy": cmd_proxy,
        "logs": cmd_logs,
        "upgrade": cmd_upgrade,
    }

    handler = commands.get(args.command or "setup")
    handler(args)


if __name__ == "__main__":
    main()

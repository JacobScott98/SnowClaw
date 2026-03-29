"""CLI entry point and argument parser."""

from __future__ import annotations

import argparse

from snowclaw import __version__
from snowclaw.commands import (
    cmd_build,
    cmd_channel,
    cmd_deploy,
    cmd_dev,
    cmd_logs,
    cmd_network,
    cmd_pull,
    cmd_push,
    cmd_restart,
    cmd_resume,
    cmd_setup,
    cmd_status,
    cmd_suspend,
    cmd_update,
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

    logs_parser = sub.add_parser("logs", help="Show container logs from the SPCS service")
    logs_parser.add_argument("-n", "--lines", type=int, default=100, help="Number of log lines (default: 100)")
    logs_parser.add_argument("--container", default="openclaw", help="Container name (default: openclaw)")
    logs_parser.add_argument("--instance", default="0", help="Instance ID (default: 0)")

    pull_parser = sub.add_parser("pull", help="Pull skills, workspace, and config from SPCS stage")
    pull_group = pull_parser.add_mutually_exclusive_group()
    pull_group.add_argument("--workspace-only", action="store_true", help="Only pull workspace/")
    pull_group.add_argument("--skills-only", action="store_true", help="Only pull skills/")
    pull_group.add_argument("--config-only", action="store_true", help="Only pull openclaw.json")

    push_parser = sub.add_parser("push", help="Push skills, workspace, and config to SPCS stage")
    push_group = push_parser.add_mutually_exclusive_group()
    push_group.add_argument("--workspace-only", action="store_true", help="Only push workspace/")
    push_group.add_argument("--skills-only", action="store_true", help="Only push skills/")
    push_group.add_argument("--config-only", action="store_true", help="Only push openclaw.json")

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
        "network": cmd_network,
        "channel": cmd_channel,
        "logs": cmd_logs,
    }

    handler = commands.get(args.command or "setup")
    handler(args)


if __name__ == "__main__":
    main()

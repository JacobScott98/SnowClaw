"""CLI entry point and argument parser."""

from __future__ import annotations

import argparse

from snowclaw import __version__
from snowclaw.commands import cmd_build, cmd_deploy, cmd_dev, cmd_pull, cmd_push, cmd_setup, cmd_update


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
    sub.add_parser("update", help="Update the OpenClaw version")

    pull_parser = sub.add_parser("pull", help="Pull skills and workspace from SPCS stage")
    pull_group = pull_parser.add_mutually_exclusive_group()
    pull_group.add_argument("--workspace-only", action="store_true", help="Only pull workspace/")
    pull_group.add_argument("--skills-only", action="store_true", help="Only pull skills/")

    push_parser = sub.add_parser("push", help="Push skills and workspace to SPCS stage")
    push_group = push_parser.add_mutually_exclusive_group()
    push_group.add_argument("--workspace-only", action="store_true", help="Only push workspace/")
    push_group.add_argument("--skills-only", action="store_true", help="Only push skills/")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "setup": cmd_setup,
        "dev": cmd_dev,
        "build": cmd_build,
        "deploy": cmd_deploy,
        "update": cmd_update,
        "pull": cmd_pull,
        "push": cmd_push,
    }

    handler = commands.get(args.command or "setup")
    handler(args)


if __name__ == "__main__":
    main()

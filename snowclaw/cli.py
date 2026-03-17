"""CLI entry point and argument parser."""

from __future__ import annotations

import argparse

from snowclaw import __version__
from snowclaw.commands import cmd_build, cmd_deploy, cmd_dev, cmd_setup, cmd_update


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
    }

    handler = commands.get(args.command or "setup")
    handler(args)


if __name__ == "__main__":
    main()

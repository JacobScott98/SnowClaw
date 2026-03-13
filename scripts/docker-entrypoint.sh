#!/bin/sh
set -e

OPENCLAW_HOME="/home/node/.openclaw"
DEFAULTS="/opt/snowclaw/defaults"

# Ensure the volume-mounted home dir exists and is writable
mkdir -p "$OPENCLAW_HOME"

# Always sync the latest config into the (possibly volume-mounted) home dir
cp -f "$DEFAULTS/openclaw.json" "$OPENCLAW_HOME/openclaw.json"

exec "$@"

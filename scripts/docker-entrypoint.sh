#!/bin/sh
set -e

OPENCLAW_HOME="/home/node/.openclaw"
DEFAULTS="/opt/snowclaw/defaults"

# Always sync the latest config into the (possibly volume-mounted) home dir
cp -f "$DEFAULTS/openclaw.json" "$OPENCLAW_HOME/openclaw.json"

# Copy plugins only if not already present (preserves runtime modifications)
mkdir -p "$OPENCLAW_HOME/plugins"
for plugin in cortex-tools cortex-code; do
  if [ ! -d "$OPENCLAW_HOME/plugins/$plugin" ]; then
    cp -r "$DEFAULTS/plugins/$plugin" "$OPENCLAW_HOME/plugins/$plugin"
  fi
done

exec "$@"

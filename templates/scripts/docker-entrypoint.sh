#!/bin/sh
set -e

OPENCLAW_HOME="/home/node/.openclaw"
DEFAULTS="/opt/snowclaw/defaults"

# Ensure the volume-mounted home dir exists and is writable
mkdir -p "$OPENCLAW_HOME"

# openclaw.json lives on the stage-backed volume — managed by deploy/push,
# not baked into the image. No copy needed here.

# Skills: only seed on first run (when dir doesn't exist)
if [ ! -d "$OPENCLAW_HOME/skills" ]; then
    cp -rf "$DEFAULTS/skills/" "$OPENCLAW_HOME/skills/"
fi

# Workspace: never copy defaults — agent creates files, user manages via pull/push
mkdir -p "$OPENCLAW_HOME/workspace"

# Auto-approve device pairing requests in the background.
# SPCS handles auth at the ingress layer, so gateway-level pairing is redundant.
(
  sleep 15
  while true; do
    # Approve all pending device pairing requests
    openclaw devices approve --latest 2>/dev/null || true
    sleep 5
  done
) &

# If GH_TOKEN is set, configure gh + git credential helper
if [ -n "$GH_TOKEN" ]; then
  gh auth setup-git 2>/dev/null || true
fi

exec "$@"

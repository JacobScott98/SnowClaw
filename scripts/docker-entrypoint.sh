#!/bin/sh
set -e

OPENCLAW_HOME="/home/node/.openclaw"
DEFAULTS="/opt/snowclaw/defaults"

# Ensure the volume-mounted home dir exists and is writable
mkdir -p "$OPENCLAW_HOME"

# Always sync the latest config into the (possibly volume-mounted) home dir
cp -f "$DEFAULTS/openclaw.json" "$OPENCLAW_HOME/openclaw.json"

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

exec "$@"

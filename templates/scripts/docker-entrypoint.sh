#!/bin/sh
set -e

OPENCLAW_HOME="/home/node/.openclaw"
DEFAULTS="/opt/snowclaw/defaults"

# Ensure the volume-mounted home dir exists
mkdir -p "$OPENCLAW_HOME"

# openclaw.json: prefer the stage-backed volume copy (managed by deploy/push).
# If none exists, seed from the baked-in default so the gateway starts with
# a valid auth config even on first boot before any push.
if [ ! -f "$OPENCLAW_HOME/openclaw.json" ] && [ -f "$DEFAULTS/openclaw.json" ]; then
    cp "$DEFAULTS/openclaw.json" "$OPENCLAW_HOME/openclaw.json"
fi

# connections.toml also lives on the stage volume — symlink it into
# the Snowflake SDK's expected location if present.
if [ -f "$OPENCLAW_HOME/connections.toml" ]; then
    mkdir -p /home/node/.snowflake
    ln -sf "$OPENCLAW_HOME/connections.toml" /home/node/.snowflake/connections.toml
    chown -R node:node /home/node/.snowflake
fi

# Skills: only seed on first run (when dir doesn't exist)
if [ ! -d "$OPENCLAW_HOME/skills" ]; then
    cp -rf "$DEFAULTS/skills/" "$OPENCLAW_HOME/skills/"
fi

# Workspace: never copy defaults — agent creates files, user manages via pull/push
mkdir -p "$OPENCLAW_HOME/workspace"

# ---------------------------------------------------------------------------
# Lock down sensitive config files so the agent (node user) cannot modify them.
# The gateway only needs read access to these files.
# ---------------------------------------------------------------------------

if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
    chown root:node "$OPENCLAW_HOME/openclaw.json" 2>/dev/null || true
    chmod 440 "$OPENCLAW_HOME/openclaw.json" 2>/dev/null || true
fi

if [ -d "$OPENCLAW_HOME/credentials" ]; then
    chown -R root:node "$OPENCLAW_HOME/credentials"
    chmod 750 "$OPENCLAW_HOME/credentials"
    find "$OPENCLAW_HOME/credentials" -type f -exec chmod 440 {} +
fi

if [ -f "$OPENCLAW_HOME/secrets.json" ]; then
    chown root:node "$OPENCLAW_HOME/secrets.json"
    chmod 440 "$OPENCLAW_HOME/secrets.json"
fi

# Ensure agent-writable directories stay owned by node
chown -R node:node "$OPENCLAW_HOME/workspace"
chown -R node:node "$OPENCLAW_HOME/skills"

# ---------------------------------------------------------------------------
# Everything below runs as the node user.
# ---------------------------------------------------------------------------

# Auto-approve device pairing requests in the background.
# SPCS handles auth at the ingress layer, so gateway-level pairing is redundant.
su -s /bin/sh node -c '
    sleep 15
    while true; do
        openclaw devices approve --latest 2>/dev/null || true
        sleep 5
    done
' &

# If GH_TOKEN is set, configure gh + git credential helper
if [ -n "$GH_TOKEN" ]; then
    su -s /bin/sh node -c 'gh auth setup-git 2>/dev/null || true'
fi

# Drop privileges and exec the gateway process as node
exec su -s /bin/sh node -c "exec $*"

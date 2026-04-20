#!/bin/sh
set -e

OPENCLAW_HOME="/home/node/.openclaw"
DEFAULTS="/opt/snowclaw/defaults"

# Ensure the volume-mounted home dir exists
mkdir -p "$OPENCLAW_HOME"

# openclaw.json lives on the stage-backed volume — managed by deploy/push,
# not baked into the image. No copy needed here.

# Render ~/.snowflake/connections.toml at startup from env vars. Cortex Code,
# snowsql, and the Snowflake Python connector all read this path.
# SNOWFLAKE_TOKEN is the runtime-scoped PAT, injected by the SPCS secret
# binding; the other fields come from service.yaml's env block.
if [ -n "$SNOWFLAKE_TOKEN" ]; then
    mkdir -p /home/node/.snowflake
    cat > /home/node/.snowflake/connections.toml <<EOF
default_connection_name = "main"

[main]
account = "$SNOWFLAKE_ACCOUNT"
user = "$SNOWFLAKE_USER"
authenticator = "PROGRAMMATIC_ACCESS_TOKEN"
token = "$SNOWFLAKE_TOKEN"
warehouse = "$SNOWFLAKE_WAREHOUSE"
database = "$SNOWFLAKE_DATABASE"
schema = "$SNOWFLAKE_SCHEMA"
role = "$SNOWFLAKE_ROLE"
EOF
    chown -R node:node /home/node/.snowflake
    chmod 400 /home/node/.snowflake/connections.toml
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

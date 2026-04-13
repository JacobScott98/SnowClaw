#!/bin/sh
# inject-gateway-auth.sh — Build-time script that generates a minimal
# openclaw.json with gateway auth configuration.  Runs during `docker build`
# so the image ships with a sensible default even when no stage-backed
# openclaw.json is mounted at runtime.
#
# If an openclaw.json already exists in the build context (copied earlier),
# this script is a no-op — we never overwrite an explicit config.

set -e

DEST="${1:-/opt/snowclaw/defaults/openclaw.json}"
AUTH_MODE="${OPENCLAW_GATEWAY_AUTH_MODE:-none}"
BIND="${OPENCLAW_GATEWAY_BIND:-loopback}"

# Skip if a config was already placed by an earlier build step
if [ -f "$DEST" ]; then
    echo "inject-gateway-auth: $DEST already exists, skipping."
    exit 0
fi

mkdir -p "$(dirname "$DEST")"

cat > "$DEST" <<EOF
{
  "gateway": {
    "auth": {
      "mode": "${AUTH_MODE}"
    },
    "bind": "${BIND}",
    "controlUi": {
      "dangerouslyAllowHostHeaderOriginFallback": true
    }
  }
}
EOF

echo "inject-gateway-auth: wrote $DEST (auth.mode=${AUTH_MODE}, bind=${BIND})"

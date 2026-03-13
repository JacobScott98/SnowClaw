#!/usr/bin/env bash
set -euo pipefail

# setup-snowflake.sh — One-time Snowflake object creation for SnowClaw.
# Runs the SQL in spcs/image-repo.sql via SnowSQL.
# Prerequisites:
#   - SnowSQL CLI installed and configured
#   - Role with CREATE DATABASE, CREATE COMPUTE POOL privileges

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "==> Creating Snowflake objects for SnowClaw..."
snowsql -f "${PROJECT_ROOT}/spcs/image-repo.sql"

echo "==> Snowflake setup complete."
echo "    Next steps:"
echo "    1. Update the secret snowclaw_secrets with your API keys"
echo "    2. Run ./scripts/deploy.sh to build and deploy"

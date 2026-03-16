#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — End-to-end SPCS deployment for SnowClaw.
# Prerequisites:
#   - Docker CLI authenticated to Snowflake image registry
#   - Snowflake objects created via setup-snowflake.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Configuration (override via environment)
SNOWFLAKE_ACCOUNT="${SNOWFLAKE_ACCOUNT:?Set SNOWFLAKE_ACCOUNT}"
SNOWFLAKE_TOKEN="${SNOWFLAKE_TOKEN:?Set SNOWFLAKE_TOKEN}"
SNOWFLAKE_REGISTRY_ACCOUNT="${SNOWFLAKE_REGISTRY_ACCOUNT:?Set SNOWFLAKE_REGISTRY_ACCOUNT (orgname-accountname)}"
IMAGE_REPO="${IMAGE_REPO:-${SNOWFLAKE_REGISTRY_ACCOUNT}.registry.snowflakecomputing.com/snowclaw_db/snowclaw_schema/snowclaw_repo}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
COMPUTE_POOL="${COMPUTE_POOL:-snowclaw_pool}"

REGISTRY_HOST="${SNOWFLAKE_REGISTRY_ACCOUNT}.registry.snowflakecomputing.com"

echo "==> Authenticating to Snowflake image registry..."
SNOWFLAKE_USER="${SNOWFLAKE_USER:?Set SNOWFLAKE_USER}"
echo "${SNOWFLAKE_TOKEN}" | docker login "${REGISTRY_HOST}" --username "${SNOWFLAKE_USER}" --password-stdin

echo "==> Building Docker image..."
docker build -t snowclaw:"${IMAGE_TAG}" "${PROJECT_ROOT}"

echo "==> Tagging for Snowflake registry..."
docker tag snowclaw:"${IMAGE_TAG}" "${IMAGE_REPO}/snowclaw:${IMAGE_TAG}"

echo "==> Pushing to Snowflake image repository..."
docker push "${IMAGE_REPO}/snowclaw:${IMAGE_TAG}"

echo "==> Creating/updating SPCS service..."
WAREHOUSE="${WAREHOUSE:-COMPUTE_WH}"
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
SLACK_APP_TOKEN="${SLACK_APP_TOKEN:-}"

python3 - "${SNOWFLAKE_ACCOUNT}" "${SNOWFLAKE_TOKEN}" "${COMPUTE_POOL}" "${PROJECT_ROOT}/spcs/service.yaml" "${WAREHOUSE}" "${OPENROUTER_API_KEY}" "${SLACK_BOT_TOKEN}" "${SLACK_APP_TOKEN}" <<'PYEOF'
import json, sys, requests

account, token, pool, spec_path, warehouse, openrouter_key, slack_bot, slack_app = sys.argv[1:9]
spec = open(spec_path).read()

url = f"https://{account}.snowflakecomputing.com/api/v2/statements"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
}

def sf_exec(sql, label=None):
    body = {"statement": sql, "timeout": 60, "database": "SNOWCLAW_DB", "schema": "SNOWCLAW_SCHEMA", "warehouse": warehouse}
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code >= 300:
        print(f"  FAIL ({resp.status_code}): {label or sql[:80]}")
        print(f"  {resp.json().get('message', resp.text)}")
        sys.exit(1)
    print(f"  OK: {label or sql[:60]}")
    return resp.json()

# Update secrets with current values from environment
secrets = {
    "snowclaw_sf_token": token,
    "snowclaw_openrouter_key": openrouter_key,
    "snowclaw_slack_bot_token": slack_bot,
    "snowclaw_slack_app_token": slack_app,
}
for name, value in secrets.items():
    if value:
        escaped = value.replace("'", "\\'")
        sf_exec(
            f"ALTER SECRET snowclaw_db.snowclaw_schema.{name} SET SECRET_STRING = '{escaped}'",
            f"UPDATE SECRET {name}",
        )

create_sql = (
    f"CREATE SERVICE IF NOT EXISTS snowclaw_db.snowclaw_schema.snowclaw_service "
    f"IN COMPUTE POOL {pool} "
    f"FROM SPECIFICATION $${spec}$$ "
    f"EXTERNAL_ACCESS_INTEGRATIONS = (snowclaw_external_access)"
)
sf_exec(create_sql, "CREATE SERVICE")

alter_sql = (
    f"ALTER SERVICE IF EXISTS snowclaw_db.snowclaw_schema.snowclaw_service "
    f"FROM SPECIFICATION $${spec}$$"
)
sf_exec(alter_sql, "ALTER SERVICE")

sf_exec("SHOW SERVICES LIKE 'snowclaw_service' IN SCHEMA snowclaw_db.snowclaw_schema", "SHOW SERVICES")

data = sf_exec("SHOW ENDPOINTS IN SERVICE snowclaw_db.snowclaw_schema.snowclaw_service", "SHOW ENDPOINTS")
for row in data.get("data", []):
    print(f"  Endpoint: {row[0]} -> {row[1]}")
PYEOF

echo "==> Done. Service deployed to SPCS."

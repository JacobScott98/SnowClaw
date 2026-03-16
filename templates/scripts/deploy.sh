#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — End-to-end SPCS deployment for SnowClaw.
# Prerequisites:
#   - Docker CLI authenticated to Snowflake image registry
#   - Snowflake objects created via setup-snowflake.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Prefix for all Snowflake object names (default: snowclaw)
PREFIX="${SNOWCLAW_PREFIX:-snowclaw}"

# Derived Snowflake object names
DB="${PREFIX}_db"
SCHEMA="${PREFIX}_schema"
FQN_SCHEMA="${DB}.${SCHEMA}"
REPO="${PREFIX}_repo"
SERVICE="${PREFIX}_service"
EXTERNAL_ACCESS="${PREFIX}_external_access"
SECRET_SF_TOKEN="${PREFIX}_sf_token"
SECRET_OPENROUTER_KEY="${PREFIX}_openrouter_key"
SECRET_SLACK_BOT_TOKEN="${PREFIX}_slack_bot_token"
SECRET_SLACK_APP_TOKEN="${PREFIX}_slack_app_token"

# Configuration (override via environment)
SNOWFLAKE_ACCOUNT="${SNOWFLAKE_ACCOUNT:?Set SNOWFLAKE_ACCOUNT}"
SNOWFLAKE_TOKEN="${SNOWFLAKE_TOKEN:?Set SNOWFLAKE_TOKEN}"
SNOWFLAKE_REGISTRY_ACCOUNT="${SNOWFLAKE_REGISTRY_ACCOUNT:?Set SNOWFLAKE_REGISTRY_ACCOUNT (orgname-accountname)}"
IMAGE_REPO="${IMAGE_REPO:-${SNOWFLAKE_REGISTRY_ACCOUNT}.registry.snowflakecomputing.com/${DB}/${SCHEMA}/${REPO}}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
COMPUTE_POOL="${COMPUTE_POOL:-${PREFIX}_pool}"

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

export DB SCHEMA FQN_SCHEMA REPO SERVICE EXTERNAL_ACCESS WAREHOUSE COMPUTE_POOL
export SECRET_SF_TOKEN SECRET_OPENROUTER_KEY SECRET_SLACK_BOT_TOKEN SECRET_SLACK_APP_TOKEN

python3 - "${PROJECT_ROOT}/spcs/service.yaml" <<'PYEOF'
import os, sys, requests

spec_path = sys.argv[1]
account = os.environ["SNOWFLAKE_ACCOUNT"]
token = os.environ["SNOWFLAKE_TOKEN"]
pool = os.environ["COMPUTE_POOL"]
warehouse = os.environ["WAREHOUSE"]
fqn_schema = os.environ["FQN_SCHEMA"]
db, schema = fqn_schema.split(".")
external_access = os.environ["EXTERNAL_ACCESS"]
service_name = os.environ["SERVICE"]
secret_sf_token = os.environ["SECRET_SF_TOKEN"]
secret_openrouter_key = os.environ["SECRET_OPENROUTER_KEY"]
secret_slack_bot_token = os.environ["SECRET_SLACK_BOT_TOKEN"]
secret_slack_app_token = os.environ["SECRET_SLACK_APP_TOKEN"]
openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
slack_bot = os.environ.get("SLACK_BOT_TOKEN", "")
slack_app = os.environ.get("SLACK_APP_TOKEN", "")

spec = open(spec_path).read()

url = f"https://{account}.snowflakecomputing.com/api/v2/statements"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
}

def sf_exec(sql, label=None):
    body = {"statement": sql, "timeout": 60, "database": db.upper(), "schema": schema.upper(), "warehouse": warehouse}
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    if resp.status_code >= 300:
        print(f"  FAIL ({resp.status_code}): {label or sql[:80]}")
        print(f"  {resp.json().get('message', resp.text)}")
        sys.exit(1)
    print(f"  OK: {label or sql[:60]}")
    return resp.json()


# Update secrets with current values from environment
secrets = {
    secret_sf_token: token,
    secret_openrouter_key: openrouter_key,
    secret_slack_bot_token: slack_bot,
    secret_slack_app_token: slack_app,
}
for name, value in secrets.items():
    if value:
        escaped = value.replace("'", "\\'")
        sf_exec(
            f"ALTER SECRET {fqn_schema}.{name} SET SECRET_STRING = '{escaped}'",
            f"UPDATE SECRET {name}",
        )

create_sql = (
    f"CREATE SERVICE IF NOT EXISTS {fqn_schema}.{service_name} "
    f"IN COMPUTE POOL {pool} "
    f"FROM SPECIFICATION $${spec}$$ "
    f"EXTERNAL_ACCESS_INTEGRATIONS = ({external_access})"
)
sf_exec(create_sql, "CREATE SERVICE")

alter_sql = (
    f"ALTER SERVICE IF EXISTS {fqn_schema}.{service_name} "
    f"FROM SPECIFICATION $${spec}$$"
)
sf_exec(alter_sql, "ALTER SERVICE")

data = sf_exec(f"SHOW ENDPOINTS IN SERVICE {fqn_schema}.{service_name}", "SHOW ENDPOINTS")
for row in data.get("data", []):
    print(f"  Endpoint: {row[0]} -> {row[1]}")
PYEOF

echo "==> Done. Service deployed to SPCS."

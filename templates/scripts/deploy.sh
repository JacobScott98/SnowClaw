#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — End-to-end SPCS deployment for SnowClaw.
# Prerequisites:
#   - Docker CLI authenticated to Snowflake image registry
#   - SnowSQL CLI installed and configured
#   - Snowflake objects created via setup-snowflake.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Configuration (override via environment)
SNOWFLAKE_ACCOUNT="${SNOWFLAKE_ACCOUNT:?Set SNOWFLAKE_ACCOUNT}"
IMAGE_REPO="${IMAGE_REPO:-${SNOWFLAKE_ACCOUNT}.registry.snowflakecomputing.com/snowclaw_db/snowclaw_schema/snowclaw_repo}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
COMPUTE_POOL="${COMPUTE_POOL:-snowclaw_pool}"

echo "==> Building Docker image..."
docker build -t snowclaw:"${IMAGE_TAG}" "${PROJECT_ROOT}"

echo "==> Tagging for Snowflake registry..."
docker tag snowclaw:"${IMAGE_TAG}" "${IMAGE_REPO}/snowclaw:${IMAGE_TAG}"

echo "==> Pushing to Snowflake image repository..."
docker push "${IMAGE_REPO}/snowclaw:${IMAGE_TAG}"

echo "==> Creating/updating SPCS service..."
snowsql -q "
  USE SCHEMA snowclaw_db.snowclaw_schema;

  CREATE SERVICE IF NOT EXISTS snowclaw_service
    IN COMPUTE POOL ${COMPUTE_POOL}
    FROM SPECIFICATION \$\$
$(cat "${PROJECT_ROOT}/spcs/service.yaml")
    \$\$
    EXTERNAL_ACCESS_INTEGRATIONS = (snowclaw_external_access);

  -- If service already exists, update the image
  ALTER SERVICE IF EXISTS snowclaw_service
    FROM SPECIFICATION \$\$
$(cat "${PROJECT_ROOT}/spcs/service.yaml")
    \$\$;
"

echo "==> Checking service status..."
snowsql -q "SHOW SERVICES LIKE 'snowclaw_service' IN SCHEMA snowclaw_db.snowclaw_schema;"

echo "==> Fetching service endpoint..."
snowsql -q "SHOW ENDPOINTS IN SERVICE snowclaw_db.snowclaw_schema.snowclaw_service;"

echo "==> Done. Service deployed to SPCS."

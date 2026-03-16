-- One-time Snowflake object creation for SnowClaw SPCS deployment.
-- Run with a role that has CREATE DATABASE / CREATE COMPUTE POOL privileges.

-- Database and schema
CREATE DATABASE IF NOT EXISTS snowclaw_db;
CREATE SCHEMA IF NOT EXISTS snowclaw_db.snowclaw_schema;

USE SCHEMA snowclaw_db.snowclaw_schema;

-- Image repository
CREATE IMAGE REPOSITORY IF NOT EXISTS snowclaw_repo;

-- Internal stage for persistent state (volume backing)
CREATE STAGE IF NOT EXISTS snowclaw_state_stage
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  DIRECTORY = (ENABLE = TRUE);

-- Secrets for environment variables (one per env var, GENERIC_STRING type)
CREATE SECRET IF NOT EXISTS snowclaw_sf_token
  TYPE = GENERIC_STRING
  SECRET_STRING = '';

CREATE SECRET IF NOT EXISTS snowclaw_openrouter_key
  TYPE = GENERIC_STRING
  SECRET_STRING = '';

CREATE SECRET IF NOT EXISTS snowclaw_slack_bot_token
  TYPE = GENERIC_STRING
  SECRET_STRING = '';

CREATE SECRET IF NOT EXISTS snowclaw_slack_app_token
  TYPE = GENERIC_STRING
  SECRET_STRING = '';

-- Network rule (references network-rules.yaml values)
CREATE OR REPLACE NETWORK RULE snowclaw_egress_rule
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = (
    'openrouter.ai:443',
    'api.slack.com:443',
    'wss-primary.slack.com:443',
    'wss-backup.slack.com:443',
    '*.snowflakecomputing.com:443'
  );

-- External access integration
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION snowclaw_external_access
  ALLOWED_NETWORK_RULES = (snowclaw_egress_rule)
  ENABLED = TRUE;

-- Compute pool
CREATE COMPUTE POOL IF NOT EXISTS snowclaw_pool
  MIN_NODES = 1
  MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_S;

-- Show image repository URL (needed for docker push)
SHOW IMAGE REPOSITORIES IN SCHEMA snowclaw_db.snowclaw_schema;

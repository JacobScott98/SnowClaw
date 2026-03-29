-- One-time Snowflake object creation for SnowClaw SPCS deployment.
-- Run with a role that has CREATE DATABASE / CREATE COMPUTE POOL privileges.
-- Replace __SNOWCLAW_DB__ and __SNOWCLAW_SCHEMA__ with your database and schema names.
-- __SNOWCLAW_PREFIX__ is derived from the database name (minus _db suffix).

-- Database and schema
CREATE DATABASE IF NOT EXISTS __SNOWCLAW_DB__;
CREATE SCHEMA IF NOT EXISTS __SNOWCLAW_DB__.__SNOWCLAW_SCHEMA__;

USE SCHEMA __SNOWCLAW_DB__.__SNOWCLAW_SCHEMA__;

-- Image repository
CREATE IMAGE REPOSITORY IF NOT EXISTS __SNOWCLAW_PREFIX___repo;

-- Internal stage for persistent state (volume backing)
CREATE STAGE IF NOT EXISTS __SNOWCLAW_PREFIX___state_stage
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  DIRECTORY = (ENABLE = TRUE);

-- Secrets for environment variables (one per env var, GENERIC_STRING type)
CREATE SECRET IF NOT EXISTS __SNOWCLAW_PREFIX___sf_token
  TYPE = GENERIC_STRING
  SECRET_STRING = '';

CREATE SECRET IF NOT EXISTS __SNOWCLAW_PREFIX___slack_bot_token
  TYPE = GENERIC_STRING
  SECRET_STRING = '';

CREATE SECRET IF NOT EXISTS __SNOWCLAW_PREFIX___slack_app_token
  TYPE = GENERIC_STRING
  SECRET_STRING = '';

-- Network rules and external access integration are managed dynamically.
-- Use `snowclaw network` to add, remove, and apply rules.
-- Rules are stored in .snowclaw/network-rules.json and applied via REST API.

-- Compute pool
CREATE COMPUTE POOL IF NOT EXISTS __SNOWCLAW_PREFIX___pool
  MIN_NODES = 1
  MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_XS;

-- Show image repository URL (needed for docker push)
SHOW IMAGE REPOSITORIES IN SCHEMA __SNOWCLAW_DB__.__SNOWCLAW_SCHEMA__;

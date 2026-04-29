# SnowClaw

Run [OpenClaw](https://github.com/openclaw/openclaw) on [Snowflake Container Services](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/overview). SnowClaw is a CLI that scaffolds, configures, and deploys a fully-wired OpenClaw instance on SPCS in minutes.

## Prerequisites

- Python 3.10+
- Docker (for local dev and building images)
- A Snowflake account with Container Services enabled
- Git

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/JacobScott98/SnowClaw/main/install.sh | bash
```

This clones the repo to `~/.snowclaw` (override with `SNOWCLAW_DIR`), installs `pipx` if needed, and registers the `snowclaw` CLI. Re-running the script updates to the latest version.

## Required Snowflake privileges

SnowClaw separates provisioning (done by **you**, with an admin role) from the running service (a low-privilege **runtime role** that you create beforehand). This section covers what each role needs.

> `ACCOUNTADMIN` has everything below and works out of the box. If that's acceptable for your account, skip ahead. The rest of this section is for tighter setups using a dedicated role.

### Step 1 — Create the runtime role

The SPCS service runs under this role. Create it once, per Snowflake account:

```sql
USE ROLE USERADMIN;
CREATE ROLE IF NOT EXISTS SNOWCLAW_RUNTIME_ROLE;
```

SnowClaw applies minimal USAGE/READ grants to this role at setup time — you don't need to pre-grant anything else.

### Step 2 — Create the admin role

Run this once, as `ACCOUNTADMIN` (or any role with `MANAGE GRANTS`), substituting `<your_user>`:

```sql
-- Create the SnowClaw admin role itself.
USE ROLE USERADMIN;
CREATE ROLE IF NOT EXISTS SNOWCLAW_ADMIN_ROLE;
GRANT ROLE SNOWCLAW_ADMIN_ROLE TO USER <your_user>;

-- Let admin impersonate runtime (needed for the transient CREATE SERVICE step).
GRANT ROLE SNOWCLAW_RUNTIME_ROLE TO ROLE SNOWCLAW_ADMIN_ROLE;

-- Account-level privileges the admin role needs for provisioning.
USE ROLE ACCOUNTADMIN;
GRANT CREATE DATABASE         ON ACCOUNT TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT CREATE COMPUTE POOL     ON ACCOUNT TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT CREATE INTEGRATION      ON ACCOUNT TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT BIND SERVICE ENDPOINT   ON ACCOUNT TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT MANAGE GRANTS           ON ACCOUNT TO ROLE SNOWCLAW_ADMIN_ROLE;

-- Mint a role-restricted PAT for the admin role.
ALTER USER <your_user> ADD PROGRAMMATIC ACCESS TOKEN snowclaw_admin_pat
  ROLE_RESTRICTION = 'SNOWCLAW_ADMIN_ROLE'
  DAYS_TO_EXPIRY = 90;
-- Copy the returned token value into `snowclaw setup` when prompted for your PAT.
```

When `snowclaw setup` asks for the admin role, enter `SNOWCLAW_ADMIN_ROLE`. When it asks for the runtime role, enter `SNOWCLAW_RUNTIME_ROLE`.

### The second token: a runtime-scoped PAT

After setup validates the runtime role, it prints an `ALTER USER ... ADD PROGRAMMATIC ACCESS TOKEN` command and prompts you to paste the resulting token. This second PAT is what lives inside the SPCS containers — it's what Cortex Code, snowsql, and the Cortex proxy use at runtime. Because it's `ROLE_RESTRICTION`-scoped to the runtime role, leaking it from inside the container only grants runtime-role privileges (no ability to alter network rules, mint secrets, or create sibling services).

The command setup prints looks like:

```sql
ALTER USER <your_user> ADD PROGRAMMATIC ACCESS TOKEN snowclaw_runtime_pat
  ROLE_RESTRICTION = 'SNOWCLAW_RUNTIME_ROLE'
  DAYS_TO_EXPIRY = 90;
```

Run it in Snowsight (or the SQL IDE of your choice) and paste the returned token into the setup prompt. SnowClaw stores it as the `{prefix}_sf_token` Snowflake secret and binds it into both containers at deploy time.

Two tokens total: the admin PAT you use on your machine (high privilege, held by the CLI and you only), and the runtime-scoped PAT inside the SPCS service (minimal privilege).

### What each admin privilege is for

| Privilege | Needed because |
|---|---|
| `CREATE DATABASE ON ACCOUNT` | `snowclaw setup` creates `{prefix}_db` (skip if you're providing an existing DB — then you need `USAGE` on it + `CREATE SCHEMA` on it instead). |
| `CREATE COMPUTE POOL ON ACCOUNT` | The SPCS service runs on its own pool (`{prefix}_pool`) to isolate billing and keep it suspendable. |
| `CREATE INTEGRATION ON ACCOUNT` | External access integrations (EAIs) are account-level objects. SnowClaw creates one (`{prefix}_external_access`) that wraps the approved network rules. |
| `BIND SERVICE ENDPOINT ON ACCOUNT` | SPCS requires this to expose the public OpenClaw endpoint on port 18789. Without it, `CREATE SERVICE` succeeds but the endpoint is unreachable. |
| `MANAGE GRANTS ON ACCOUNT` | Lets the admin role grant privileges onward to the runtime role (including `BIND SERVICE ENDPOINT` and `DATABASE ROLE SNOWFLAKE.CORTEX_USER`) and handle the transient `CREATE SERVICE` grant during deploy. |

### Using a pre-existing database/schema

If you already have a database and schema you want SnowClaw to live in, skip `CREATE DATABASE ON ACCOUNT` and grant these on the existing objects instead:

```sql
USE ROLE ACCOUNTADMIN;
GRANT USAGE                    ON DATABASE <your_db> TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT USAGE                    ON SCHEMA   <your_db>.<your_schema> TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT CREATE STAGE             ON SCHEMA   <your_db>.<your_schema> TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT CREATE IMAGE REPOSITORY  ON SCHEMA   <your_db>.<your_schema> TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT CREATE SECRET            ON SCHEMA   <your_db>.<your_schema> TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT CREATE NETWORK RULE      ON SCHEMA   <your_db>.<your_schema> TO ROLE SNOWCLAW_ADMIN_ROLE;
GRANT CREATE SERVICE           ON SCHEMA   <your_db>.<your_schema> TO ROLE SNOWCLAW_ADMIN_ROLE;
```

### Why `SYSADMIN` alone isn't enough

`SYSADMIN` holds `CREATE DATABASE` by default but not `CREATE INTEGRATION`, `CREATE COMPUTE POOL`, or `BIND SERVICE ENDPOINT` — those require `ACCOUNTADMIN` or an explicit grant. If you enter `SYSADMIN` at the admin-role prompt without first granting the privileges above, `snowclaw setup` will fail when it tries to create the compute pool or the EAI.

### Runtime role privileges (managed by SnowClaw — for reference)

You don't need to configure these yourself — `snowclaw setup` and `snowclaw deploy` apply them for you. They're listed here so you can audit what the runtime PAT actually carries inside the container:

- `USAGE` on database, schema, compute pool, EAI
- `READ`+`WRITE` on the state stage (for workspace file I/O)
- `READ` on the image repository (to pull container images)
- `MONITOR` on the compute pool (for `snowclaw status`)
- `READ` on each channel/tool/custom secret (what SPCS's `CREATE SERVICE` actually checks when resolving `snowflakeSecret:` bindings — `USAGE` is the wrong privilege here despite looking right by name)
- `BIND SERVICE ENDPOINT ON ACCOUNT` (required because the service exposes a public endpoint on port 18789 — without this `CREATE SERVICE` fails with "Please grant BIND SERVICE ENDPOINT to service owner role")
- `OWNERSHIP` of the SPCS service itself (acquired by `CREATE SERVICE`, which runs under the runtime role with a transient `CREATE SERVICE` grant that's revoked immediately after)

Explicitly **not** granted to the runtime role: any privilege on the network rule, `CREATE NETWORK RULE`, `CREATE SECRET`, `CREATE INTEGRATION`, permanent `CREATE SERVICE`, `CREATE COMPUTE POOL`, `USAGE` on other pools or warehouses, or `OWNERSHIP` of anything other than its own service. A compromised runtime PAT inside the container cannot alter network rules, mint new secrets, or spin up sibling services.

## Quick Start

```bash
# 1. Create a new project directory
mkdir my-openclaw && cd my-openclaw

# 2. Run the interactive setup wizard
snowclaw setup

# 3. Start locally
snowclaw dev
```

The setup wizard prompts for your Snowflake account, credentials, roles, communication channels, and optionally provisions all Snowflake objects via REST API. Once complete, open `http://localhost:18789` for the OpenClaw UI.

## Security

SnowClaw is designed to be safe to run in your Snowflake account by default.

- **Network egress control** — All outbound traffic is deny-by-default. Only explicitly approved hosts (managed via `snowclaw network`) get Snowflake network rules and external access integrations. The container cannot reach the internet unless you allow it.
- **SPCS ingress control** — Snowflake handles TLS termination and authentication on the single public endpoint. There are no open ports or exposed services beyond what SPCS declares — the ingress surface is managed entirely by Snowflake's infrastructure.
- **File permissions** — `openclaw.json` and credential files are root-owned and read-only at runtime. The agent cannot modify config.
- **Role separation** — The CLI uses your admin role for provisioning; the SPCS service runs under a dedicated low-privilege runtime role (which you create beforehand and pass to `snowclaw setup`). The runtime PAT inside the container is `ROLE_RESTRICTION`-scoped to this role, so a compromised agent cannot alter network rules, mint new secrets, or create sibling services. See [Required Snowflake privileges](#required-snowflake-privileges) for the exact privilege split.
- **Secret masking** — The Cortex proxy scans all outbound LLM messages and replaces known secret values with `[REDACTED:VAR_NAME]`. Credentials never reach the model.
- **User-managed secrets** — `CUSTOM_`-prefixed env vars in `.env` become individual Snowflake secrets, mounted at runtime — never baked into the image.

## Features

- **One-command setup** — Interactive wizard collects credentials, generates config, and optionally provisions all Snowflake objects via REST API. No snowsql required.
- **Secrets & credential management** — All secrets live in your local `.env` file and are uploaded to Snowflake as individual SECRET objects on every `deploy` or `push`. Nothing is baked into the Docker image. Add any custom environment variable to `.env` and it automatically becomes a Snowflake secret, mounted into the container at runtime. See [Secrets & Credentials](#secrets--credentials) for details.
- **Snowflake Cortex LLMs** — Pre-configured as a provider. Models run inside Snowflake — your data never leaves your account.
- **Cortex Code** — AI coding assistant installed automatically in the container with a bundled skill definition. Your agent can write, edit, and manage code out of the box.
- **Cortex proxy sidecar** — A FastAPI proxy between OpenClaw and Cortex with two endpoints: `/v1/chat/completions` (OpenAI-shaped, for OpenAI/Snowflake/Llama models) and `/v1/messages` (Anthropic-shaped, for Claude models with native prompt caching). Handles parallel tool call serialization, request normalization, secret masking, response metadata logging, and automatic 1M context window headers for Claude. Can also be deployed as a [standalone proxy](#standalone-proxy) for external OpenClaw agents.
- **Multi-channel messaging** — Slack, Telegram, and Discord supported out of the box. `snowclaw channel add` walks you through configuration with auto-detected network rules per channel.
- **Dynamic network rules** — Auto-detects required external hosts from your config and prompts for approval before creating Snowflake network rules and external access integrations.
- **Build hooks** — Drop `.sh` scripts into `build-hooks/` to install packages or tools at image build time. No Dockerfile editing needed.
- **Local dev and SPCS deployment** — `snowclaw dev` runs everything locally with Docker Compose. `snowclaw deploy` builds, pushes, and creates the SPCS service in one step. `snowclaw push` and `snowclaw pull` sync config and workspace without rebuilding.

## Architecture

```
Channels <--socket/ws--> SPCS Ingress <--> OpenClaw Gateway (:18789)
                                             |
                                             +-- Web UI
                                             +-- WebSocket RPC
                                             +-- /v1/* OpenAI-compatible API
                                             +-- Plugin HTTP routes
                                             |
                                           Cortex Proxy Sidecar (:8080)
                                             +-- POST /v1/chat/completions
                                             |     (OpenAI/Snowflake/Llama — tool call serialization, request transforms)
                                             +-- POST /v1/messages
                                             |     (Claude — native prompt caching, no transforms needed)
                                             +-- Secret masking (per-shape walker)
                                             +-- Response metadata logging
                                             +-- 1M context window headers (Claude)
                                             |
                                             +-- Snowflake Cortex (outbound)
```

All traffic goes through a single SPCS ingress endpoint on port 18789. The Cortex proxy runs as a sidecar container in the same service with two endpoints — one per API shape. Snowflake handles TLS and authentication.

## Secrets & Credentials

SnowClaw manages secrets entirely through your local `.env` file. Secrets are **never baked into the Docker image** — they're uploaded to Snowflake as individual SECRET objects and mounted into the running container at runtime.

### How it works

1. **`snowclaw setup`** collects your Snowflake PAT, channel tokens, and tool credentials during the interactive wizard and writes them to `.env`.
2. **`snowclaw deploy`** and **`snowclaw push`** automatically read `.env`, create or update a Snowflake SECRET for each variable, and regenerate the SPCS service spec to mount them.
3. At runtime, secrets are available as environment variables inside the container.

### What becomes a secret

| Source | Example | How it's handled |
|--------|---------|-----------------|
| Snowflake auth | `SNOWFLAKE_TOKEN` | Held as the `{prefix}_sf_token` Snowflake secret — a user-provided PAT `ROLE_RESTRICTION`-scoped to the runtime role. Bound into both containers. The openclaw container renders `/home/node/.snowflake/connections.toml` from it at startup so Cortex Code / snowsql / the Python connector find it. The proxy uses it for Cortex REST. Because the PAT is role-restricted, leaking it only grants runtime-role access — no network rule changes, no secret creation, no sibling services. |
| Channel credentials | `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN` | Added via `snowclaw channel add`, auto-detected per channel |
| Tool credentials | `GH_TOKEN`, `BRAVE_API_KEY` | Added via setup wizard when tools are selected |
| Custom variables | Any other `KEY=value` in `.env` | Automatically becomes a Snowflake secret — no config needed |

**Any key=value pair you add to `.env` is automatically uploaded as a Snowflake secret.** There's no separate registration step. Just add the variable, run `snowclaw push`, and it's available in the container.

### Updating secrets

```bash
# Edit .env with your new or changed values, then:
snowclaw push              # pushes config, workspace, skills, AND secrets
snowclaw push --secrets    # push ONLY secrets (skips file sync for speed)
```

Both commands update the Snowflake SECRET objects and restart the service to pick up changes.

### Secret masking

The Cortex proxy sidecar scans all outbound LLM messages and replaces known secret values with `[REDACTED:VAR_NAME]`. This means your credentials never reach the model, even if the agent tries to include them in a prompt.

Variables listed in `SNOWCLAW_MASK_VARS` (a comma-separated list in `.env`) are added to the masking set. By default, all token and API key variables are masked.

### File permissions at runtime

- `openclaw.json`, `secrets.json`, and the `credentials/` directory are **root-owned and read-only** (mode `440`). The agent process cannot modify configuration or credentials.
- The `.snowflake/` directory (containing `connections.toml`) is owned by the `node` user so Cortex can read and write connection state.
- `skills/` is agent-writable. The agent's `workspace/` lives only on the SPCS stage / container volume — it is not scaffolded locally and is not part of `push` / `pull`. Move files in and out with `snowclaw upload` / `download` / `ls`.

## CLI Commands

| Command | Description |
|---------|-------------|
| `snowclaw setup` | Interactive wizard — scaffolds project, collects credentials, writes config |
| `snowclaw setup --force` | Re-run setup, overwriting template files |
| `snowclaw dev` | Build and run locally with Docker Compose |
| `snowclaw build` | Build the Docker image without deploying |
| `snowclaw deploy` | Build, push to Snowflake registry, and create/update the SPCS service |
| `snowclaw status` | Show service status, endpoints, and compute pool state |
| `snowclaw suspend` | Suspend the SPCS service and compute pool |
| `snowclaw resume` | Resume the SPCS compute pool and service |
| `snowclaw restart` | Restart the service to pick up config changes |
| `snowclaw logs` | Show container logs from the SPCS service (add `-f`/`--tail` to follow) |
| `snowclaw update` | Update the OpenClaw base image version |
| `snowclaw push` | Push skills and openclaw.json (and secrets) to SPCS stage |
| `snowclaw push --secrets` | Update only Snowflake secrets and connections.toml (skip file sync) |
| `snowclaw pull` | Pull skills and openclaw.json from SPCS stage |
| `snowclaw ls [path]` | List files in the SPCS workspace (paths workspace-relative) |
| `snowclaw upload <local> [--dest <subdir>] [--force]` | Upload a local file into the SPCS workspace (live — agent sees it immediately) |
| `snowclaw download <stage-path> [--dest <local-dir>]` | Download a file from the SPCS workspace |
| `snowclaw network list` | Show current approved network rules |
| `snowclaw network add <host>` | Add a network rule (prompts to apply) |
| `snowclaw network remove <host>` | Remove a network rule |
| `snowclaw network detect` | Auto-detect required rules from project config |
| `snowclaw network apply` | Push current rules to Snowflake |
| `snowclaw channel list` | Show configured channels |
| `snowclaw channel add` | Interactive wizard to add a channel |
| `snowclaw channel edit <name>` | Edit a channel's credentials |
| `snowclaw channel remove <name>` | Remove a channel |
| `snowclaw plugins list` | Show configured plugins |
| `snowclaw plugins add <spec>` | Add a plugin (npm package or local path) |
| `snowclaw plugins remove <id>` | Remove a plugin |
| `snowclaw proxy setup` | Interactive wizard for standalone Cortex proxy |
| `snowclaw proxy deploy` | Build, push, and deploy the standalone proxy to SPCS |
| `snowclaw proxy status` | Show standalone proxy service status and endpoint |
| `snowclaw proxy suspend` | Suspend the standalone proxy service and compute pool |
| `snowclaw proxy resume` | Resume the standalone proxy compute pool and service |
| `snowclaw proxy logs` | Show standalone proxy container logs |

`push` and `pull` accept `--skills-only` or `--config-only` to sync selectively. Workspace files are intentionally outside `push`/`pull` — use `snowclaw upload` / `download` / `ls` (all paths are relative to the workspace root).

## Standalone Proxy

The standalone proxy deploys just the Cortex proxy as its own SPCS service with a public endpoint. This lets external OpenClaw agents (running outside Snowflake) access Cortex LLMs through a lightweight gateway — no full SnowClaw deployment required.

### Setup

```bash
mkdir my-proxy && cd my-proxy
snowclaw proxy setup    # collects Snowflake credentials, creates objects
snowclaw proxy deploy   # builds, pushes, and deploys the proxy service
```

After deploying, `snowclaw proxy deploy` prints the public endpoint URL and a ready-to-use OpenClaw provider config.

### How it works

Each user authenticates to the SPCS endpoint with their own Snowflake PAT. SPCS ingress validates the token and injects `Sf-Context-Current-User` for traceability, but strips the `Authorization` header before it reaches the container. To get the PAT through to Cortex, OpenClaw sends it in a custom `X-Cortex-Token` header which SPCS passes through untouched. The proxy reads this header and forwards it to Cortex as a Bearer token.

The proxy exposes both endpoints:

- **`/v1/chat/completions`** — OpenAI-shaped, for OpenAI/Snowflake/Llama models (also accepts Claude via Cortex's translation layer, but without native caching)
- **`/v1/messages`** — Anthropic-shaped, for Claude models with native prompt caching support

### OpenClaw provider config

Add this to your external OpenClaw's `openclaw.json`. Use two providers to route Claude models through the Messages endpoint (for native caching) and everything else through chat completions:

```json5
{
  models: {
    providers: {
      "cortex-claude": {
        baseUrl: "https://<proxy-endpoint>",
        apiKey: "${SNOWFLAKE_TOKEN}",
        headers: {
          "X-Cortex-Token": "${SNOWFLAKE_TOKEN}"
        },
        api: "anthropic-messages"
      },
      "cortex-openai": {
        baseUrl: "https://<proxy-endpoint>/v1",
        apiKey: "${SNOWFLAKE_TOKEN}",
        headers: {
          "X-Cortex-Token": "${SNOWFLAKE_TOKEN}"
        },
        api: "openai-completions"
      }
    }
  }
}
```

- `cortex-claude` — routes Claude models via `/v1/messages` with native `cache_control` marker support
- `cortex-openai` — routes OpenAI, Snowflake, and Llama models via `/v1/chat/completions`
- `apiKey` authenticates with SPCS ingress (sent as `Authorization: Snowflake Token="..."`)
- `X-Cortex-Token` passes through ingress to the proxy, which forwards it to Cortex
- Each user's PAT provides per-user identity and traceability via `Sf-Context-Current-User`

### Features

- **Dual endpoints** — `/v1/chat/completions` for OpenAI-shaped requests, `/v1/messages` for Anthropic-shaped requests with native prompt caching
- **No shared secrets** — each user sends their own PAT, no service-level token needed
- **Per-user traceability** — SPCS injects `Sf-Context-Current-User` automatically
- **Rate limit retry** — exponential backoff on Cortex 429 responses
- **Request transforms** — parallel tool call serialization, max_tokens normalization (chat completions only)
- **1M context window** — automatic `anthropic-beta` header injection for Claude models
- **Response metadata logging** — opt-in via `PROXY_LOG_RESPONSES` env var (usage stats, errors, cache hit rates)
- **Minimal footprint** — single container on CPU_X64_XS compute pool

## License

Apache-2.0

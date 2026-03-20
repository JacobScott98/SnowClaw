# SnowClaw

Run [OpenClaw](https://github.com/openclaw/openclaw) on [Snowflake Container Services](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/overview) with zero forking. SnowClaw is a CLI that scaffolds, configures, and deploys a fully-wired OpenClaw instance — complete with Snowflake-native LLM providers, Slack integration, and Cortex Code — in minutes.

## Features

- **One-command setup** — Interactive wizard collects credentials, generates config, and optionally provisions all Snowflake objects (database, image repo, compute pool, secrets) via REST API. No snowsql required.
- **Dynamic network rules** — Auto-detects required external hosts from your config (providers, Slack) and prompts for approval before creating Snowflake network rules. Add custom hosts on the fly with `snowclaw network add`.
- **Snowflake Cortex LLMs** — Pre-configured as a provider. Models run inside Snowflake with no data leaving your account.
- **Slack integration** — Socket mode out of the box — no public webhook URL needed, which is ideal for SPCS.
- **Cortex Code** — Installed automatically in the container image, with a bundled skill definition for immediate use.
- **Local dev mode** — `snowclaw dev` builds and runs everything locally with Docker Compose for fast iteration.
- **SPCS deployment** — `snowclaw deploy` builds the image, pushes to your Snowflake registry, and creates/updates the service in one step.
- **Workspace sync** — `snowclaw push` and `snowclaw pull` sync your local skills and workspace files to/from SPCS stage storage.
- **No fork needed** — Builds on the official OpenClaw image. Your config, skills, and plugins are layered on top at build time.
- **Persistent state** — SPCS stage-backed volume keeps conversations, skills, and workspace data across container restarts.

## Prerequisites

- Python 3.10+
- Docker (for local dev and building images)
- A Snowflake account with Container Services enabled
- Git

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/JacobScott98/SnowClaw/main/install.sh | bash
```

This clones the repo to `~/snowclaw` (override with `SNOWCLAW_DIR`), installs `pipx` if needed, and registers the `snowclaw` CLI. Re-running the script updates to the latest version.

## Quick Start

```bash
# 1. Create a new project directory
mkdir my-openclaw && cd my-openclaw

# 2. Run the interactive setup wizard
snowclaw setup

# 3. Start locally
snowclaw dev
```

The setup wizard will prompt for:
- Snowflake account, username, and PAT token
- Slack bot and app tokens (optional)
- Whether to create Snowflake objects (database, compute pool, etc.)

Once complete, open `http://localhost:18789` to access the OpenClaw UI.

## CLI Commands

| Command | Description |
|---------|-------------|
| `snowclaw setup` | Interactive wizard — scaffolds project, collects credentials, writes config |
| `snowclaw setup --force` | Re-run setup, overwriting template files |
| `snowclaw dev` | Build and run locally with Docker Compose |
| `snowclaw build` | Build the Docker image without deploying |
| `snowclaw deploy` | Build, push to Snowflake registry, and create/update the SPCS service |
| `snowclaw update` | Update the OpenClaw base image version |
| `snowclaw pull` | Pull skills and workspace from SPCS stage to local |
| `snowclaw push` | Push local skills and workspace to SPCS stage |
| `snowclaw network list` | Show current approved network rules |
| `snowclaw network add <host>` | Add a network rule (prompts to apply to Snowflake) |
| `snowclaw network remove <host>` | Remove a network rule |
| `snowclaw network detect` | Auto-detect required rules from project config |
| `snowclaw network apply` | Push current rules to Snowflake |

`snowclaw pull` and `snowclaw push` accept `--workspace-only` or `--skills-only` to sync selectively.

`snowclaw network add` accepts `host` or `host:port` (default port 443) and an optional `--reason` flag.

## Project Structure

After running `snowclaw setup`, your project directory looks like this:

```
my-openclaw/
  .snowclaw/              # Project marker and build artifacts
    config.json           # Project metadata (version, prefix, etc.)
    network-rules.json    # Approved network rules for external access
  .env                    # Secrets — gitignored
  .gitignore
  openclaw.json           # OpenClaw configuration (providers, channels, agents)
  connections.toml        # Snowflake connection — gitignored
  skills/                 # Editable skill definitions
    cortex-code/
  workspace/              # Markdown knowledge base
```

Edit `openclaw.json` to add agents, change model routing, or configure channels. Add markdown files to `workspace/` to give your agents reference material. Skills in `skills/` are synced into the container at build time.

## Model Providers

**Snowflake Cortex** is always enabled. It uses your Snowflake account credentials to call Cortex LLMs — your data stays within Snowflake.

## Network Rules

Snowflake Container Services blocks all outbound traffic by default. SnowClaw manages network rules dynamically so your service can reach external APIs.

**During setup**, the CLI auto-detects which hosts are required based on your config (e.g., Slack WebSocket endpoints if Slack is enabled, `*.snowflakecomputing.com:443` for Cortex) and prompts you to approve them before creating the Snowflake objects.

**During deploy**, the CLI re-checks your config against saved rules and prompts for approval if anything changed (e.g., you added a new provider).

**Manual management** is available for hosts the CLI can't auto-detect:

```bash
# Add a custom API endpoint
snowclaw network add api.example.com --reason "Custom API"

# Add with a non-standard port
snowclaw network add db.example.com:5432 --reason "External database"

# View current rules
snowclaw network list

# Remove a rule
snowclaw network remove api.example.com

# Push current rules to Snowflake
snowclaw network apply
```

Rules are saved in `.snowclaw/network-rules.json` (committed to git) and compiled into a single Snowflake `NETWORK RULE` + `EXTERNAL ACCESS INTEGRATION` when applied.

## Deploying to SPCS

```bash
snowclaw deploy
```

This will:
1. Assemble the build context (Dockerfile, config, plugins, skills)
2. Build the Docker image
3. Push it to your Snowflake image repository
4. Create or update the SPCS service

The service runs on a `CPU_X64_S` compute pool with 1-2 CPUs and 2-4 GiB RAM. State is persisted to an internal stage mounted as a volume.

After deployment, use `snowclaw push` and `snowclaw pull` to sync skills and workspace content without rebuilding.

## Architecture

```
Slack <--socket--> SPCS Ingress <--> OpenClaw Gateway (:18789)
                                       |
                                       +-- Web UI
                                       +-- WebSocket RPC
                                       +-- /v1/* OpenAI-compatible API
                                       +-- Plugin HTTP routes
                                       |
                                       +-- Snowflake Cortex (outbound)
```

All traffic goes through a single SPCS ingress endpoint on port 18789. Snowflake handles TLS and authentication.

## License

Apache-2.0

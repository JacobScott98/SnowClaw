# SnowClaw

Run [OpenClaw](https://github.com/openclaw/openclaw) on [Snowflake Container Services](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/overview) with zero forking. SnowClaw is a CLI that scaffolds, configures, and deploys a fully-wired OpenClaw instance — complete with Snowflake Cortex LLMs, multi-channel messaging, and a Cortex-compatible proxy — in minutes.

## Features

- **One-command setup** — Interactive wizard collects credentials, generates config, and optionally provisions all Snowflake objects via REST API. No snowsql required.
- **Snowflake Cortex LLMs** — Pre-configured as a provider. Models run inside Snowflake — your data never leaves your account.
- **Cortex proxy sidecar** — A FastAPI proxy sits between OpenClaw and Cortex, fixing parallel tool call issues for Claude models and normalizing request parameters across model families. Also masks secrets in outbound LLM traffic so credentials never reach the model.
- **Multi-channel messaging** — Slack, Telegram, and Discord supported out of the box. `snowclaw channel add` walks you through configuration with auto-detected network rules per channel.
- **Dynamic network rules** — Auto-detects required external hosts from your config and prompts for approval before creating Snowflake network rules and external access integrations.
- **Config lockdown** — Sensitive files (`openclaw.json`, credentials) are root-owned and read-only at runtime. The agent can read config but cannot modify it. Defense-in-depth via `workspaceOnly` tool policy.
- **Admin/service role separation** — CLI operations use your admin role; the deployed container runs under a least-privilege service role.
- **Build hooks** — Drop `.sh` scripts into `build-hooks/` to install packages or tools at image build time. No Dockerfile editing needed.
- **User-managed secrets** — Add `CUSTOM_`-prefixed vars to `.env` and they become individual Snowflake secrets, mounted into the container, and auto-masked by the proxy.
- **Cortex Code** — Installed automatically in the container image with a bundled skill definition.
- **Local dev mode** — `snowclaw dev` builds and runs everything locally with Docker Compose.
- **SPCS deployment** — `snowclaw deploy` builds the image, pushes to your Snowflake registry, and creates/updates the service in one step.
- **Config and workspace sync** — `snowclaw push` and `snowclaw pull` sync skills, workspace, and `openclaw.json` to/from SPCS stage storage — no rebuild required.
- **Persistent state** — SPCS stage-backed volume keeps conversations, skills, and workspace data across container restarts.
- **No fork needed** — Builds on the official OpenClaw image. Your config, skills, and plugins are layered on top.

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
- Admin and service roles
- Communication channels (Slack, Telegram, Discord)
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
| `snowclaw status` | Show service status, endpoints, and compute pool state |
| `snowclaw suspend` | Suspend the SPCS service and compute pool |
| `snowclaw resume` | Resume the SPCS compute pool and service |
| `snowclaw restart` | Restart the service to pick up config changes |
| `snowclaw logs` | Show container logs from the SPCS service |
| `snowclaw update` | Update the OpenClaw base image version |
| `snowclaw push` | Push skills, workspace, and config to SPCS stage |
| `snowclaw pull` | Pull skills, workspace, and config from SPCS stage |
| `snowclaw network list` | Show current approved network rules |
| `snowclaw network add <host>` | Add a network rule (prompts to apply) |
| `snowclaw network remove <host>` | Remove a network rule |
| `snowclaw network detect` | Auto-detect required rules from project config |
| `snowclaw network apply` | Push current rules to Snowflake |
| `snowclaw channel list` | Show configured channels |
| `snowclaw channel add` | Interactive wizard to add a channel |
| `snowclaw channel edit <name>` | Edit a channel's credentials |
| `snowclaw channel remove <name>` | Remove a channel |

`push` and `pull` accept `--workspace-only`, `--skills-only`, or `--config-only` to sync selectively.

## Project Structure

After running `snowclaw setup`:

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
  build-hooks/            # Custom build scripts (*.sh, run at image build time)
  workspace/              # Markdown knowledge base
```

## Security

SnowClaw applies multiple layers of protection in the deployed container:

- **File permissions** — `openclaw.json` and credential files are owned by root with read-only access for the gateway process. The agent cannot modify config at runtime.
- **Tool policy** — `workspaceOnly` restricts native file tools to the workspace directory.
- **Role separation** — The CLI uses your admin role for infrastructure. The container runs under a dedicated service role with minimal privileges.
- **Secret masking** — The Cortex proxy scans all outbound LLM messages and replaces known secret values with `[REDACTED:VAR_NAME]`.
- **User secrets** — `CUSTOM_`-prefixed env vars in `.env` become individual Snowflake secrets, mounted at runtime (never baked into the image).

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
                                             +-- Parallel tool call serialization
                                             +-- Secret masking
                                             +-- Request normalization
                                             |
                                             +-- Snowflake Cortex (outbound)
```

All traffic goes through a single SPCS ingress endpoint on port 18789. The Cortex proxy runs as a sidecar container in the same service. Snowflake handles TLS and authentication.

## License

Apache-2.0

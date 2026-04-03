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
- **Role separation** — The CLI uses your admin role for infrastructure operations. The deployed container runs under a dedicated service role with minimal privileges.
- **Secret masking** — The Cortex proxy scans all outbound LLM messages and replaces known secret values with `[REDACTED:VAR_NAME]`. Credentials never reach the model.
- **User-managed secrets** — `CUSTOM_`-prefixed env vars in `.env` become individual Snowflake secrets, mounted at runtime — never baked into the image.

## Features

- **One-command setup** — Interactive wizard collects credentials, generates config, and optionally provisions all Snowflake objects via REST API. No snowsql required.
- **Snowflake Cortex LLMs** — Pre-configured as a provider. Models run inside Snowflake — your data never leaves your account.
- **Cortex Code** — AI coding assistant installed automatically in the container with a bundled skill definition. Your agent can write, edit, and manage code out of the box.
- **Cortex proxy sidecar** — A FastAPI proxy between OpenClaw and Cortex that serializes parallel tool calls (fixing Claude model issues), normalizes request parameters across model families, and masks secrets in outbound traffic.
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
                                             +-- Parallel tool call serialization
                                             +-- Secret masking
                                             +-- Request normalization
                                             |
                                             +-- Snowflake Cortex (outbound)
```

All traffic goes through a single SPCS ingress endpoint on port 18789. The Cortex proxy runs as a sidecar container in the same service. Snowflake handles TLS and authentication.

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

## License

Apache-2.0

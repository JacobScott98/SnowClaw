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
- **Secrets & credential management** — All secrets live in your local `.env` file and are uploaded to Snowflake as individual SECRET objects on every `deploy` or `push`. Nothing is baked into the Docker image. Add any custom environment variable to `.env` and it automatically becomes a Snowflake secret, mounted into the container at runtime. See [Secrets & Credentials](#secrets--credentials) for details.
- **Snowflake Cortex LLMs** — Pre-configured as a provider. Models run inside Snowflake — your data never leaves your account.
- **Cortex Code** — AI coding assistant installed automatically in the container with a bundled skill definition. Your agent can write, edit, and manage code out of the box.
- **Cortex proxy sidecar** — A FastAPI proxy between OpenClaw and Cortex that serializes parallel tool calls (fixing Claude model issues), normalizes request parameters across model families, and masks secrets in outbound traffic. Can also be deployed as a [standalone proxy](#standalone-proxy) for external OpenClaw agents.
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

## Secrets & Credentials

SnowClaw manages secrets entirely through your local `.env` file. Secrets are **never baked into the Docker image** — they're uploaded to Snowflake as individual SECRET objects and mounted into the running container at runtime.

### How it works

1. **`snowclaw setup`** collects your Snowflake PAT, channel tokens, and tool credentials during the interactive wizard and writes them to `.env`.
2. **`snowclaw deploy`** and **`snowclaw push`** automatically read `.env`, create or update a Snowflake SECRET for each variable, and regenerate the SPCS service spec to mount them.
3. At runtime, secrets are available as environment variables inside the container.

### What becomes a secret

| Source | Example | How it's handled |
|--------|---------|-----------------|
| Snowflake auth | `SNOWFLAKE_TOKEN` | Created as a dedicated secret during deploy |
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
- `workspace/` and `skills/` are agent-writable.

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
| `snowclaw push` | Push skills, workspace, config, and secrets to SPCS stage |
| `snowclaw push --secrets` | Update only Snowflake secrets and connections.toml (skip file sync) |
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
| `snowclaw proxy setup` | Interactive wizard for standalone Cortex proxy |
| `snowclaw proxy deploy` | Build, push, and deploy the standalone proxy to SPCS |
| `snowclaw proxy status` | Show standalone proxy service status and endpoint |
| `snowclaw proxy suspend` | Suspend the standalone proxy service and compute pool |
| `snowclaw proxy resume` | Resume the standalone proxy compute pool and service |
| `snowclaw proxy logs` | Show standalone proxy container logs |

`push` and `pull` accept `--workspace-only`, `--skills-only`, or `--config-only` to sync selectively.

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

### OpenClaw provider config

Add this to your external OpenClaw's `openclaw.json`:

```json5
{
  models: {
    providers: {
      cortex: {
        baseUrl: "https://<proxy-endpoint>/v1",
        apiKey: "${SNOWFLAKE_TOKEN}",
        headers: {
          "X-Cortex-Token": "${SNOWFLAKE_TOKEN}"
        },
        api: "openai-completions",
        models: [
          { id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6" },
          { id: "claude-opus-4-6", name: "Claude Opus 4.6" }
        ]
      }
    }
  }
}
```

- `apiKey` authenticates with SPCS ingress (sent as `Authorization: Snowflake Token="..."`)
- `X-Cortex-Token` passes through ingress to the proxy, which forwards it to Cortex
- Each user's PAT provides per-user identity and traceability via `Sf-Context-Current-User`

### Features

- **No shared secrets** — each user sends their own PAT, no service-level token needed
- **Per-user traceability** — SPCS injects `Sf-Context-Current-User` automatically
- **Rate limit retry** — exponential backoff on Cortex 429 responses
- **Request transforms** — parallel tool call serialization, max_tokens normalization
- **Minimal footprint** — single container on CPU_X64_XS compute pool

## License

Apache-2.0

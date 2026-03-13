# SnowClaw PRD

## Overview

SnowClaw is an open-source bootstrapper for running OpenClaw on Snowflake Container Services (SPCS), preconfigured with Snowflake-native LLM providers, Slack integration, and Cortex Code interop. It is a separate repo that layers deployment config, custom plugins, and SPCS infrastructure on top of upstream OpenClaw — no fork required.

## Goals

- Host the OpenClaw gateway on SPCS and expose it via Snowflake ingress
- Integrate with Slack as the primary messaging channel
- Use OpenRouter and Snowflake Cortex-hosted LLMs as model providers
- Enable bidirectional communication with Cortex Code (via MCP, local executable, or skill)
- Ship out-of-the-box Cortex Code capability as a registered tool
- Stay upstream-compatible — pin to OpenClaw releases, upgrade independently

## Architecture

### Approach: Separate Bootstrapper Repo

SnowClaw is not a fork. It is a standalone repo that:

1. Extends the official OpenClaw Docker image with Snowflake-specific config and plugins
2. Provides SPCS service specs, network rules, and deployment scripts
3. Registers custom plugins for Cortex tools and Cortex Code integration
4. Contributes general-purpose improvements upstream via PR when gaps are found

### Repo Structure

```
snowclaw/
  Dockerfile              # FROM openclaw, layers config + plugins
  spcs/
    service.yaml          # SPCS service spec
    network-rules.yaml    # Ingress/egress rules
    image-repo.sql        # Snowflake image repository setup
  config/
    openclaw.json         # Preconfigured: OpenRouter + Cortex providers, Slack, auth
  plugins/
    cortex-tools/         # OpenClaw plugin: Cortex SQL/query tools
    cortex-code/          # OpenClaw plugin: MCP bridge for Cortex Code
  cli/
    deploy.sh             # End-to-end SPCS deployment
    setup-snowflake.sh    # One-time Snowflake object creation
  snowclaw.py
```

### CLI

Interactive and clean CLI installer that asks for required parameters and sets configs appropriately.

Parameters will include:

- ACCOUNT_LOCATOR: For connecting to Snowflake
- PAT: For connecting to Snowflake
- Model Selector: Select which models you want to run

Steps the main CLI workflow will take:

1. Input account locator and pat into models configuration of openclaw.json
2. Create connections.toml using inputs
3. Other inputs privilege inputs (security settings)
4. Create necessary external access integration (Using Snowflake Rest API)
5. Create compute pool
6. Create snowpark container service

Other workflows:

1. Update openclaw version


### Custom UI

We will want to build a custom UI that is better at showcasing data results from SQL queries and visualizations.

### Security Profiles

Run with preset security profiles

### Out of the Box Snowflake Skills

Should grab all the skills from Cortex Code

### How It Connects

```
Slack <--webhook/socket--> SPCS Ingress <--> OpenClaw Gateway (single port)
                                                |
                                                +-- Control UI (same origin)
                                                +-- WebSocket RPC (same origin)
                                                +-- /v1/* OpenAI-compat API (same origin)
                                                +-- Plugin HTTP routes (same origin)
                                                |
                                                +-- OpenRouter (outbound HTTPS)
                                                +-- Snowflake Cortex LLMs (outbound HTTPS)
                                                +-- Cortex Code MCP (plugin route or bridge)
```

Everything the frontend talks to is same-origin. The OpenClaw gateway serves UI, API, and WebSocket on a single port. SPCS exposes one ingress endpoint (e.g. `https://snowclaw-service.snowflakecomputing.app`). Snowflake overrides CORS on all traffic, so same-origin is required and satisfied by default.

## Model Providers

### OpenRouter (config-only, no plugin needed)

OpenRouter is OpenAI-compatible. Config:

```json
{
  "models": {
    "providers": {
      "openrouter": {
        "baseUrl": "https://openrouter.ai/api/v1",
        "apiKey": "$OPENROUTER_API_KEY",
        "api": "openai-completions",
        "models": []
      }
    }
  }
}
```

### Snowflake Cortex LLMs (config-only, no plugin needed)

Cortex Complete is OpenAI-compatible. Config:

```json
{
  "models": {
    "providers": {
      "cortex": {
        "baseUrl": "https://<account>.snowflakecomputing.com/api/v2/cortex/chat/completions",
        "apiKey": "$SNOWFLAKE_TOKEN",
        "api": "openai-completions",
        "models": [
          {
            "id": "snowflake-arctic",
            "name": "Snowflake Arctic",
            "contextWindow": 4096,
            "maxTokens": 4096
          }
        ]
      }
    }
  }
}
```

## Slack Integration

Built-in OpenClaw extension. No custom code needed. Config:

```json
{
  "channels": {
    "slack": {
      "enabled": true,
      "mode": "socket",
      "accounts": {
        "default": {
          "botToken": "$SLACK_BOT_TOKEN",
          "appToken": "$SLACK_APP_TOKEN"
        }
      }
    }
  }
}
```

Socket mode preferred for SPCS (no public webhook URL needed). HTTP mode also works if SPCS ingress is configured to route `/api/channels/slack/events`.

## Cortex Code Integration

Two custom plugins to build:

### 1. cortex-tools (OpenClaw plugin)

Registers tools via `api.registerTool()` that give agents the ability to:

- Execute Cortex SQL queries
- Call Cortex functions (COMPLETE, TRANSLATE, SUMMARIZE, etc.)
- Query Snowflake tables

### 2. cortex-code (OpenClaw plugin)

Bridges Cortex Code and the OpenClaw gateway. Options:

- **MCP server**: Plugin exposes an MCP endpoint via `api.registerHttpRoute()` that Cortex Code connects to as an MCP server
- **Local CLI bridge**: Small executable that translates between Cortex Code and OpenClaw WebSocket RPC
- **Skill**: OpenClaw skill that wraps Cortex Code commands

Decision on which approach deferred to implementation phase.

## Persistence (SPCS Volumes)

OpenClaw stores state at `~/.openclaw/`. SPCS volumes are backed by Snowflake internal stages.

### What must persist across container restarts

| Path | Contents | Size | Notes |
|------|----------|------|-------|
| `credentials/` | OAuth tokens, secrets | < 1 MB | Security-critical |
| `agents/<id>/sessions/` | Conversation transcripts | 50-500 MB+ | Grows over time |
| `agents/<id>/agent/auth-profiles.json` | Per-agent auth state | < 1 MB | API keys, rotation |

### What can be baked into the image

| Path | Contents | Notes |
|------|----------|-------|
| `openclaw.json` | Config | Static for a given deployment |
| `workspace/` | Agent skills, identity files | Stable between deploys |

### Volume setup

```yaml
# SPCS service spec
spec:
  containers:
    - name: openclaw
      volumeMounts:
        - name: openclaw-state
          mountPath: /home/node/.openclaw
  volumes:
    - name: openclaw-state
      source: "@snowclaw_state_stage"
```

### Session maintenance (prevent unbounded growth)

```json
{
  "agents": {
    "defaults": {
      "session": {
        "maintenance": {
          "maxDiskBytes": "500mb",
          "pruneAfter": "30d",
          "rotateBytes": "10mb",
          "maxEntries": 500
        }
      }
    }
  }
}
```

## UI Customization

If the default Control UI needs modification for SPCS:

1. **Swap asset root**: Set `gateway.controlUi.root` to a custom-built UI directory in the container
2. **Change base path**: Set `gateway.controlUi.basePath` if SPCS routes under a prefix
3. **Disable and replace**: Set `gateway.controlUi.enabled: false` and serve a custom UI via plugin HTTP route

No monkey-patching or forking. If a needed config knob is missing, PR it upstream.

## Phased Rollout

### Phase 1: Open Source Bootstrapper

- Repo with Dockerfile, SPCS specs, preconfigured `openclaw.json`
- OpenRouter + Cortex LLM providers (config-only)
- Slack channel (built-in)
- Persistent volume on Snowflake stage
- Blog post + demo video

### Phase 2: Cortex Code Plugins + Community

- Build cortex-tools and cortex-code plugins
- Upstream PRs for any gaps found in OpenClaw plugin API
- Talks (Snowflake Summit, meetups), community engagement

### Phase 3: Snowflake Native App (pivot)

- Repackage as a Snowflake Native App (managed distribution via Snowflake Marketplace)
- Proprietary managed layer: multi-tenancy, billing, audit logging, SSO, compliance
- Separate private codebase from the open-source bootstrapper
- Session storage backed by Snowflake tables instead of .jsonl files on stage

## IP and Trademark Considerations

- Pick product name and file USPTO trademark early (before building in public)
- Grab domain, GitHub org, and social handles before any public announcement
- Open-source bootstrapper: MIT or Apache 2.0 license (maximizes adoption)
- Native app (Phase 3): separate proprietary codebase, no shared code with OSS repo
- Do not build proprietary value into the open-source repo
- Consult an IP attorney before going public (~$200-400 for initial consultation)

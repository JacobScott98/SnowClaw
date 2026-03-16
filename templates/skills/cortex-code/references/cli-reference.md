# Cortex Code CLI Reference

Complete reference for the `cortex` CLI. The main skill file covers the most common usage patterns — this file has the full details.

## All CLI flags

| Flag | Description |
|------|-------------|
| `-p, --print "prompt"` | Batch mode: pass prompt, print response, exit |
| `-f, --file <path>` | Read prompt from file, execute, exit |
| `-c, --connection <name>` | Use a named connection from `connections.toml` |
| `-w, --workdir <path>` | Set working directory for file operations |
| `-m, --model <model>` | Override AI model selection |
| `--output-format stream-json` | Structured JSON output for scripting |
| `--plan` | Require approval before all actions |
| `--bypass` | Auto-approve all planned actions |
| `--dangerously-allow-all-tool-calls` | Disable all permission prompts |
| `--continue` | Resume most recent conversation |
| `-r, --resume <id>` | Resume a specific session (or `last`) |
| `--private` | Disable session saving |
| `-V, --version` | Show version |
| `--help` | Show help |

## Subcommands

| Command | Description |
|---------|-------------|
| `cortex update [version]` | Update to latest or specific version |
| `cortex mcp list` | List configured MCP servers |
| `cortex mcp add <name> <cmd> [args]` | Add an MCP server |
| `cortex mcp get <server>` | Show MCP server details |
| `cortex mcp remove <name>` | Remove an MCP server |
| `cortex mcp start <server>` | Start/test an MCP server |
| `cortex skill list` | List available skills |
| `cortex skill add <path_or_url>` | Add a skill |
| `cortex skill remove <path>` | Remove a skill |
| `cortex completion bash\|zsh\|fish` | Generate shell completions |

## Available models

`auto` (recommended), `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-4-sonnet`, `openai-gpt-5.2`

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Configuration error |
| 3 | Connection error |
| 4 | Permission denied |
| 130 | User interrupt (Ctrl+C) |

## Built-in tools

Cortex Code has its own tool suite that it uses autonomously:

**Snowflake tools:**
- `SnowflakeSqlExecute` — run SQL queries with caching and token refresh
- `SnowflakeObjectSearch` — semantic search across tables, views, schemas, databases, functions
- `SnowflakeProductDocs` — search Snowflake documentation
- `ReflectSemanticModel` — validate Cortex Analyst models
- `SnowflakeMultiCortexAnalyst` — natural language to SQL

**File tools:** Read, Write, Edit, Glob, Grep

**Shell:** Bash (with background execution, streaming, 2min default / 10min max timeout)

**Web:** WebSearch, WebFetch (require Snowsight account setting)

**Agents:** RunSubagent (general-purpose, explore, plan, custom)

## Configuration paths

| Path | Purpose |
|------|---------|
| `~/.snowflake/connections.toml` | Snowflake connections |
| `~/.snowflake/cortex/settings.json` | Settings |
| `~/.snowflake/cortex/permissions.json` | Permission preferences |
| `~/.snowflake/cortex/mcp.json` | MCP server configs |
| `~/.snowflake/cortex/conversations/` | Saved sessions |
| `~/.snowflake/cortex/skills/` | Global skills |
| `~/.snowflake/cortex/agents/` | Custom agents |
| `~/.snowflake/cortex/memory/` | Memory storage |

Project-level config: `.cortex/` or `.claude/` directory with `skills/`, `agents/`, `settings.json`.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `SNOWFLAKE_HOME` | Override default `~/.snowflake` directory |
| `CORTEX_AGENT_MODEL` | Override model selection |
| `CORTEX_ENABLE_MEMORY` | Enable memory tool (`true` or `1`) |
| `COCO_DANGEROUS_MODE_REQUIRE_SQL_WRITE_PERMISSION` | Require confirmation for SQL writes |

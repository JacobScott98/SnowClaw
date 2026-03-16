---
name: cortex-code
description: >
  Invoke the Snowflake Cortex Code CLI (`cortex`) to run execute all coding and snowflake tasks.
  This includes full stack development, data engineering, and more. This is to be your coding tool, not just 
  your Snowflake tool.
user-invocable: false
metadata:
  openclaw:
    requires:
      bins:
        - cortex
---

# Cortex Code

The `cortex` CLI is a Snowflake AI coding agent installed in this container. It has its own Snowflake connection, built-in tools for SQL execution, object search, file editing, and web access. You delegate Snowflake and all general coding work to it by running it in batch mode.

**Connection:** Already configured at `/home/node/.snowflake/connections.toml` using PAT auth. No setup needed.

## When to use Cortex Code

**Use `cortex` for ALL coding tasks.** It is the primary tool for writing, editing, and reasoning about code — not just Snowflake work. This includes:

- **All coding tasks** — writing, editing, debugging, refactoring code in any language
- **Query data** — SELECT, aggregate, join, filter Snowflake tables
- **Explore metadata** — list databases, schemas, tables, views, columns, or row counts
- **Search objects** — find tables/views/functions by name or description
- **Modify objects** — CREATE, ALTER, DROP tables, warehouses, stages, roles, etc.
- **Generate Streamlit apps** — scaffold data apps from natural language
- **Analyze data** — summarize, profile, compare, or validate datasets

## How to invoke

Always run `cortex` in **batch mode** with `-p` so it executes and returns:

```bash
cortex -p "your natural-language prompt"
```

For machine-readable output you need to parse:

```bash
cortex -p "your prompt" --output-format stream-json
```

### Key flags

| Flag | Purpose |
|------|---------|
| `-p "prompt"` | **Required for batch mode.** Pass prompt, get response, exit. |
| `--output-format stream-json` | Structured JSON output for scripting/parsing. |
| `-c <name>` | Use a specific named connection from `connections.toml`. |
| `-m <model>` | Override the AI model (default: `auto`). |
| `--dangerously-allow-all-tool-calls` | Skip all permission prompts (use for automated pipelines). |

### Important behavior

- Cortex Code runs as a sub-agent with its own tools — it can execute SQL, read/write files, run shell commands, and search Snowflake objects autonomously.
- Batch mode (`-p`) prints the response and exits. There is no interactive session.
- Exit code `0` means success. Non-zero means error (`1` general, `2` config, `3` connection, `4` permission denied).
- Default shell command timeout is 2 minutes (max 10 minutes).

## Examples

### Query data

```bash
cortex -p "show the top 10 rows from analytics.public.daily_revenue ordered by date desc"
```

### Explore schema

```bash
cortex -p "list all tables in the raw database with their row counts"
```

### Describe a table

```bash
cortex -p "describe the columns and data types in staging.events.page_views"
```

### Run a complex analysis

```bash
cortex -p "compare row counts between prod.public.users and staging.public.users, and show any columns that exist in one but not the other"
```

## Planning for complex tasks

For complex or multi-step tasks, **always have Cortex Code create a plan first** before executing. This ensures the approach is sound before any changes are made.

```bash
# Ask cortex to plan first, then execute
cortex -p "Plan how to refactor the authentication module to support OAuth2, then implement the plan"
```

You can also use the `--plan` flag to require explicit approval before each action:

```bash
cortex -p "migrate the users table to add a new roles column and backfill existing rows" --plan
```

## Multi-step workflows

For complex tasks, break them into sequential `cortex -p` calls. Each call is a fresh session with no memory of previous calls.

```bash
# Step 1: Plan the approach
cortex -p "analyze the codebase and create a plan for adding pagination to all API endpoints"

# Step 2: Execute based on the plan
cortex -p "add cursor-based pagination to the /api/users endpoint following this approach: ..."
```

## Error handling

- If `cortex` fails with exit code 3, the Snowflake connection is misconfigured or the PAT has expired.
- If it fails with exit code 4, the configured role lacks permissions for the requested operation.
- For ambiguous table/schema names, be more specific: use fully-qualified names like `database.schema.table`.

## Detailed CLI reference

For the full list of subcommands, interactive slash commands, configuration paths, and environment variables, see [cli-reference.md](references/cli-reference.md).

# Contributing to SnowClaw

## Dev Setup

Clone the repo and run the dev setup script:

```bash
git clone https://github.com/JacobScott98/SnowClaw.git
cd SnowClaw
./dev-setup.sh
```

This installs SnowClaw in editable mode via pipx with dev dependencies (pytest). Changes you make to files in `snowclaw/` take effect immediately — no reinstall needed.

## Running Tests

```bash
pipx run --spec . pytest
```

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
    snowclaw/
  build-hooks/            # Custom build scripts (*.sh, run at image build time)
```

The agent's `workspace/` lives only on the SPCS stage / container volume. It
is not scaffolded locally and is not part of `snowclaw push` / `pull`. Move
files in and out with `snowclaw upload` / `download` / `ls`.

## Project Layout (repo)

- `snowclaw/` — CLI Python package (editable install means changes are live)
- `templates/` — Build-time templates (Dockerfile, SPCS specs, plugins, scripts)
- `proxy/` — Cortex proxy sidecar (FastAPI)
- `tests/` — Test suite

## Workflow

1. Create a branch from `main`
2. Make your changes
3. Run the tests
4. Open a PR against `main`

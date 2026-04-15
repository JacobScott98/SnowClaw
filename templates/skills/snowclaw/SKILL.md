---
name: snowclaw
description: >
  Reference for advising the user on SnowClaw features. Covers how files move
  between the user's machine, this container, and the SPCS stage; how
  push/pull is scoped; and where to point the user for managing channels,
  plugins, network rules, and the cortex proxy. Load the linked references on
  demand — most sections are a one-line pointer.
user-invocable: false
---

# SnowClaw

You are running inside a SnowClaw deployment on Snowflake Container Services
(SPCS). The user manages this deployment with the `snowclaw` CLI on their host
machine. Use this skill when the user asks how to do something with SnowClaw
itself (move files, change config, restart, manage channels, etc.) — not for
day-to-day coding work.

## File transfer (user ↔ workspace)

Two transports are available; pick based on direction and whether the user is
in chat or at a terminal.

- **You (in container) → user (download via chat link)** — generate a presigned
  URL the user can click. This is the only download path that works without
  the user touching their terminal. Tokens, code template, gotchas:
  [references/file-transfer.md](references/file-transfer.md).
- **User (host) → workspace (upload)** — `snowclaw upload <local-path>
  [--dest <subdir>] [--force]`. Live-mounted; you see the file immediately.
- **Workspace → user (host download)** — `snowclaw download <workspace-path>
  [--dest <local-dir>]`.
- **List workspace contents** — `snowclaw ls [path]`. Default lists
  workspace root.

All `upload` / `download` / `ls` paths are relative to `workspace/` — the user
never specifies the `workspace/` prefix. When you tell the user where you put
a new file, give the workspace-relative path (e.g. `report.csv`, not
`/home/node/.openclaw/workspace/report.csv`).

For multi-file or large transfers, see the tarball recipe in
[references/file-transfer.md](references/file-transfer.md).

## Push / pull semantics

`snowclaw push` and `snowclaw pull` only sync `skills/` and `openclaw.json`
between the user's project directory and the SPCS stage. **Workspace files
are deliberately not part of push/pull** — they can grow large and contain
agent-generated artifacts. Use `upload`/`download`/`ls` for workspace files.

Flags: `--skills-only`, `--config-only` for either direction; `push --secrets`
to refresh Snowflake secrets without touching files.

## Other SnowClaw areas

For each of these, point the user at `snowclaw <subcommand> --help` rather
than guessing at flags:

- **Channels** (Slack / Telegram / Discord) — `snowclaw channel {list, add, remove, edit}`.
- **Plugins** — `snowclaw plugins {list, add, remove}`.
- **Network rules** (SPCS egress) — `snowclaw network {list, add, remove, detect, apply}`.
- **Lifecycle** — `snowclaw {status, suspend, resume, restart, logs}`.
- **Standalone Cortex proxy** — `snowclaw proxy {setup, deploy, status, suspend, resume, logs}`.

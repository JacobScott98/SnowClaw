# File Transfer

Two ways to move files between the user's machine and the SnowClaw workspace.
Use the **presigned URL** path when you (the in-container agent) need to hand
the user a downloadable file from chat. Use the **host-side CLI** path when
the user is at a terminal — it's simpler, scriptable, and doesn't expose a
public URL.

---

## 1. Presigned URLs from inside the container

The workspace at `/home/node/.openclaw/workspace/` is an s3fs mount backed by
a Snowflake internal stage. You can generate a time-limited presigned download
URL for any file in the workspace and hand it to the user in chat.

### Use the right token

There are two tokens available in the container:

- `/snowflake/session/token` — the SPCS service identity. Generates URLs that
  point at an internal S3 access point (`spcs-ap-*-s3alias.s3...`). **These
  return AccessDenied from outside.**
- `$SNOWFLAKE_TOKEN` env var — the user's PAT (Programmatic Access Token).
  Generates URLs through Snowflake's customer-facing S3 path
  (`sfc-prod3-*-customer-stage.s3...`). **These work externally.**

You must use `$SNOWFLAKE_TOKEN` with the `PROGRAMMATIC_ACCESS_TOKEN`
authenticator and an explicit `user=$SNOWFLAKE_USER`.

### Generate a download URL

```python
import os
import snowflake.connector

conn = snowflake.connector.connect(
    account=os.environ['SNOWFLAKE_ACCOUNT'],
    user=os.environ['SNOWFLAKE_USER'],
    authenticator='PROGRAMMATIC_ACCESS_TOKEN',
    token=os.environ['SNOWFLAKE_TOKEN'],
    database=os.environ.get('SNOWCLAW_DB'),
    schema=os.environ.get('SNOWCLAW_SCHEMA'),
    warehouse=os.environ.get('SNOWFLAKE_WAREHOUSE'),
)

url = conn.cursor().execute("""
    SELECT GET_PRESIGNED_URL(
        @<state_stage>,
        '<stage_path>',
        3600  -- expiry in seconds (max 3600)
    )
""").fetchone()[0]
```

Substitute `<state_stage>` with the project's state stage (visible in
`connections.toml` / setup logs; typically `<prefix>_state_stage`).

### Stage path mapping

The workspace mount is:

- Filesystem: `/home/node/.openclaw/workspace/`
- Stage: `@<prefix>_state_stage`
- Path on stage: `workspace/<filename>`

So `/home/node/.openclaw/workspace/foo.txt` → stage path `workspace/foo.txt`.

### Large files / directories

```bash
tar czf <name>.tar.gz --exclude='.git' <directory>/
```

(`zip` is not installed.) The tarball lands in the workspace and is
immediately visible on the stage via s3fs. Generate a presigned URL for the
tarball and clean it up after the user confirms download.

### Gotchas

- s3fs is slow for recursive operations (`find`, large `ls -R`). Keep listings
  shallow.
- `GET_PRESIGNED_URL` with the SPCS session token produces URLs that look
  valid but return AccessDenied externally — the bucket policy explicitly
  denies external access from the SPCS IAM identity. Always use the user's PAT.
- URLs expire (max 3600s). Generate fresh ones as needed.
- `BUILD_SCOPED_FILE_URL` and `BUILD_STAGE_FILE_URL` exist but require
  Snowflake session auth to access — not useful for direct browser downloads.

---

## 2. Host-side CLI commands

When the user is at a terminal, recommend these instead — no URL handling, no
expiry, no token confusion. All paths are relative to `workspace/`; the user
never types the `workspace/` prefix.

### `snowclaw ls [path]`

List files under `workspace/<path>` (or workspace root if `path` is omitted).
Cheap; safe to run repeatedly. Output is a Rich table with name, size, and
md5.

```
snowclaw ls
snowclaw ls data/
```

### `snowclaw download <stage-path> [--dest <local-dir>]`

Pull a single workspace file to the user's machine. `--dest` defaults to the
current working directory. The file keeps its basename.

```
snowclaw download report.csv
snowclaw download data/report.csv --dest ./out/
```

### `snowclaw upload <local-path> [--dest <subpath>] [--force]`

Push a single local file into the workspace. The workspace volume is
**live-mounted** — once upload finishes, the file is visible at
`/home/node/.openclaw/workspace/...` immediately. No restart required.

- `--dest` is a *subdirectory* under `workspace/`. The local filename is
  preserved (`PUT` cannot rename in flight).
- Without `--force`, the CLI prompts to confirm if a file already exists at
  the target path.
- Directory uploads are not supported in v1; tar/zip first if you need to
  send a tree.

```
snowclaw upload report.csv                    # → workspace/report.csv
snowclaw upload report.csv --dest data/       # → workspace/data/report.csv
snowclaw upload report.csv --force            # silent overwrite
```

### How to advise the user

When you place a file the user should download, tell them the workspace path
and offer them the choice:

> I've written the analysis to `reports/q1-summary.md` in your workspace. You
> can pull it down with `snowclaw download reports/q1-summary.md`, or I can
> generate a presigned URL if you'd rather click a link.

Don't invent an `inbox/` or other naming convention — just be explicit about
where you put the file.

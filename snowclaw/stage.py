"""Stage file operations for Snowflake internal stages."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Redirect SNOWFLAKE_HOME before importing snowflake.connector so it doesn't
# find and permission-check ~/.snowflake/connections.toml at import time.
# SnowClaw passes connection params directly and doesn't use the global config.
_orig_sf_home = os.environ.get("SNOWFLAKE_HOME")
_tmp_sf_home = tempfile.mkdtemp()
os.environ["SNOWFLAKE_HOME"] = _tmp_sf_home

import snowflake.connector  # noqa: E402

# Restore original SNOWFLAKE_HOME
if _orig_sf_home is None:
    os.environ.pop("SNOWFLAKE_HOME", None)
else:
    os.environ["SNOWFLAKE_HOME"] = _orig_sf_home
try:
    os.rmdir(_tmp_sf_home)
except OSError:
    pass


def get_sf_connection(
    account: str,
    user: str,
    token: str,
    warehouse: str | None = None,
    database: str | None = None,
    schema: str | None = None,
) -> snowflake.connector.SnowflakeConnection:
    """Create a Snowflake connection using PAT auth."""
    return snowflake.connector.connect(
        account=account,
        user=user,
        token=token,
        authenticator="programmatic_access_token",
        warehouse=warehouse,
        database=database,
        schema=schema,
    )


def stage_list(
    conn: snowflake.connector.SnowflakeConnection,
    fqn_stage: str,
    prefix: str = "",
) -> list[dict]:
    """List files on a stage. Returns list of {name, size, md5}."""
    path = f"@{fqn_stage}/{prefix}" if prefix else f"@{fqn_stage}"
    cur = conn.cursor()
    try:
        cur.execute(f"LIST {path}")
        rows = cur.fetchall()
    finally:
        cur.close()
    return [{"name": row[0], "size": row[1], "md5": row[2]} for row in rows]


def stage_file_exists(
    conn: snowflake.connector.SnowflakeConnection,
    fqn_stage: str,
    stage_path: str,
) -> bool:
    """Check whether a specific file exists at `stage_path` on the stage."""
    files = stage_list(conn, fqn_stage, prefix=stage_path)
    return any(files)


def stage_pull_file(
    conn: snowflake.connector.SnowflakeConnection,
    fqn_stage: str,
    stage_path: str,
    local_dir: str,
) -> None:
    """Download a single file from stage to a local directory."""
    cur = conn.cursor()
    try:
        cur.execute(f"GET @{fqn_stage}/{stage_path} file://{local_dir}/")
    finally:
        cur.close()


def stage_push_file(
    conn: snowflake.connector.SnowflakeConnection,
    fqn_stage: str,
    local_path: str,
    stage_path: str,
) -> None:
    """Upload a single file to stage."""
    # stage_path is the directory prefix on the stage
    cur = conn.cursor()
    try:
        cur.execute(
            f"PUT file://{local_path} @{fqn_stage}/{stage_path} "
            f"AUTO_COMPRESS=FALSE OVERWRITE=TRUE"
        )
    finally:
        cur.close()


def pull_directory(
    conn: snowflake.connector.SnowflakeConnection,
    fqn_stage: str,
    stage_prefix: str,
    local_dir: Path,
) -> list[str]:
    """Download all files under a stage prefix to a local directory.

    Returns list of downloaded file paths (relative to local_dir).
    """
    files = stage_list(conn, fqn_stage, prefix=stage_prefix)
    downloaded = []
    for f in files:
        # f["name"] is like "stage_name/prefix/subdir/file.txt"
        # Strip the stage name prefix to get the relative path
        full_name = f["name"]
        # LIST returns paths like "stage_name/prefix/path" — strip everything
        # up to and including the stage_prefix
        parts = full_name.split("/", 1)
        if len(parts) < 2:
            continue
        stage_rel = parts[1]  # everything after the stage name
        # Strip the stage_prefix from the beginning
        if stage_rel.startswith(stage_prefix):
            rel_path = stage_rel[len(stage_prefix):].lstrip("/")
        else:
            rel_path = stage_rel

        if not rel_path:
            continue

        dest_dir = local_dir / Path(rel_path).parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        stage_pull_file(conn, fqn_stage, stage_rel, str(dest_dir))
        downloaded.append(rel_path)
    return downloaded


def push_directory(
    conn: snowflake.connector.SnowflakeConnection,
    fqn_stage: str,
    stage_prefix: str,
    local_dir: Path,
) -> list[str]:
    """Upload all files in a local directory to a stage prefix.

    Returns list of uploaded file paths (relative to local_dir).
    """
    uploaded = []
    for dirpath, _, filenames in os.walk(local_dir):
        for filename in filenames:
            if filename.startswith("."):
                continue
            local_path = Path(dirpath) / filename
            rel = local_path.relative_to(local_dir)
            # Stage path is prefix/relative_parent_dir
            stage_path = f"{stage_prefix}/{rel.parent}" if str(rel.parent) != "." else stage_prefix
            stage_push_file(conn, fqn_stage, str(local_path), stage_path)
            uploaded.append(str(rel))
    return uploaded

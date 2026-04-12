"""Dump raw SSE chunks from the Cortex proxy to see exactly what's in the stream.

Usage:
    PROXY_URL=http://localhost:8090 python tests/raw_stream_dump.py [messages|completions]

Defaults to testing both endpoints.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

import httpx

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8080")
MODEL = "claude-sonnet-4-6"


def load_pat() -> str:
    pat = os.environ.get("SNOWFLAKE_PAT")
    if pat:
        return pat
    toml_path = Path.home() / ".snowflake" / "connections.toml"
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        default_name = data.get("default_connection_name", "")
        conn = data.get(default_name) if default_name else None
        if conn is None:
            conn = next((v for v in data.values() if isinstance(v, dict)), {})
        if isinstance(conn, dict):
            pat = conn.get("token") or conn.get("password") or ""
            if pat:
                return pat
    print("ERROR: No PAT found.")
    sys.exit(1)


def dump_stream(pat: str, endpoint: str, body: dict) -> None:
    url = f"{PROXY_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
    }

    print(f"\nStreaming from {url} (model={MODEL})")
    print("=" * 80)

    chunk_num = 0
    with httpx.stream("POST", url, json=body, headers=headers, timeout=120.0) as resp:
        print(f"HTTP {resp.status_code}")
        print(f"Response headers:")
        for k, v in resp.headers.items():
            print(f"  {k}: {v}")
        print("=" * 80)

        if resp.status_code != 200:
            print(resp.read().decode())
            return

        for raw_line in resp.iter_lines():
            chunk_num += 1
            print(f"\n--- chunk {chunk_num} ---")
            print(repr(raw_line))

    print(f"\n{'=' * 80}")
    print(f"Total chunks (lines) received: {chunk_num}")


def main() -> None:
    pat = load_pat()
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"

    if mode in ("completions", "both"):
        print("\n>>> CHAT COMPLETIONS ENDPOINT (/v1/chat/completions)")
        dump_stream(pat, "/v1/chat/completions", {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Say hello in exactly 3 words."}],
            "max_completion_tokens": 64,
            "stream": True,
        })

    if mode in ("messages", "both"):
        print("\n\n>>> MESSAGES ENDPOINT (/v1/messages)")
        dump_stream(pat, "/v1/messages", {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Say hello in exactly 3 words."}],
            "max_tokens": 64,
            "stream": True,
        })


if __name__ == "__main__":
    main()

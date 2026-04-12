"""Quick smoke test for the standalone Cortex proxy endpoint.

Runs against a deployed proxy (SPCS or local) and exercises both proxy surfaces:
  - POST /v1/chat/completions  (OpenAI shape, all models)
  - POST /v1/messages          (Anthropic Messages shape, Claude only)

Auth flow:
  - Authorization: Snowflake Token="<PAT>" → authenticates with SPCS ingress (stripped)
  - X-Cortex-Token: <PAT>                  → passes through to proxy, forwarded to Cortex as Bearer

Usage:
  python tests/smoke_proxy_endpoint.py                # runs all scenarios
  python tests/smoke_proxy_endpoint.py --scenario chat
  python tests/smoke_proxy_endpoint.py --scenario messages
  python tests/smoke_proxy_endpoint.py --scenario stream
  python tests/smoke_proxy_endpoint.py --scenario parallel_tools

PAT is read from tests/.env (line `SNOWFLAKE_PAT=...`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

PROXY_BASE_URL = "https://mrdbfdb-bqecbew-nyb92647.snowflakecomputing.app"
CHAT_ENDPOINT = f"{PROXY_BASE_URL}/v1/chat/completions"
MESSAGES_ENDPOINT = f"{PROXY_BASE_URL}/v1/messages"

CLAUDE_MODEL = "claude-sonnet-4-6"


def _load_pat() -> str:
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(f"Missing {env_path} — create it with SNOWFLAKE_PAT=<your-pat>")
        sys.exit(1)
    for line in env_path.read_text().splitlines():
        if line.startswith("SNOWFLAKE_PAT="):
            return line.split("=", 1)[1].strip()
    print("SNOWFLAKE_PAT not found in tests/.env")
    sys.exit(1)


def _auth_headers(pat: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f'Snowflake Token="{pat}"',
        "X-Cortex-Token": pat,
    }


def _check_html_login(resp: requests.Response) -> bool:
    """Detect SPCS HTML login page (auth failure). Returns True if it was an HTML response."""
    if "text/html" in resp.headers.get("content-type", ""):
        print("\nGot HTML login page — SPCS ingress auth failed.")
        return True
    return False


# ---------------------------------------------------------------------------
# Scenario 1: chat completions (OpenAI shape) — original smoke test
# ---------------------------------------------------------------------------


def run_chat_completions(pat: str) -> bool:
    print(f"=== Scenario: chat completions ({CHAT_ENDPOINT}) ===")
    payload = {
        "model": CLAUDE_MODEL,
        "messages": [{"role": "user", "content": "Say hello in exactly 5 words."}],
        "max_tokens": 64,
        "stream": False,
    }
    try:
        resp = requests.post(CHAT_ENDPOINT, headers=_auth_headers(pat), json=payload, timeout=60)
    except requests.ConnectionError as exc:
        print(f"Connection failed: {exc}")
        return False

    print(f"Status: {resp.status_code}")
    if _check_html_login(resp) or resp.status_code != 200:
        print(f"Error body: {resp.text[:500]}")
        return False

    data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print(f"Assistant: {msg}\n")
    return bool(msg)


# ---------------------------------------------------------------------------
# Scenario 2: Messages basic chat
# ---------------------------------------------------------------------------


def run_messages_basic(pat: str) -> bool:
    print(f"=== Scenario: messages basic ({MESSAGES_ENDPOINT}) ===")
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Say hello in exactly 5 words."}],
    }
    try:
        resp = requests.post(MESSAGES_ENDPOINT, headers=_auth_headers(pat), json=payload, timeout=60)
    except requests.ConnectionError as exc:
        print(f"Connection failed: {exc}")
        return False

    print(f"Status: {resp.status_code}")
    if _check_html_login(resp) or resp.status_code != 200:
        print(f"Error body: {resp.text[:500]}")
        return False

    data = resp.json()
    blocks = data.get("content") or []
    text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")
    usage = data.get("usage", {})
    print(f"Assistant: {text}")
    print(f"Usage: input={usage.get('input_tokens')} output={usage.get('output_tokens')}\n")
    return bool(text)


# ---------------------------------------------------------------------------
# Scenario 3: Messages streaming
# ---------------------------------------------------------------------------


def run_messages_streaming(pat: str) -> bool:
    print(f"=== Scenario: messages streaming ({MESSAGES_ENDPOINT}) ===")
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": "Count to 5 separated by commas."}],
    }
    try:
        resp = requests.post(
            MESSAGES_ENDPOINT, headers=_auth_headers(pat), json=payload, timeout=60, stream=True,
        )
    except requests.ConnectionError as exc:
        print(f"Connection failed: {exc}")
        return False

    print(f"Status: {resp.status_code}")
    if _check_html_login(resp) or resp.status_code != 200:
        print(f"Error body: {resp.text[:500]}")
        return False

    saw_message_start = False
    saw_message_stop = False
    text_chunks: list[str] = []

    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[len("data: "):])
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "message_start":
            saw_message_start = True
        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                text_chunks.append(delta.get("text", ""))
        elif etype == "message_stop":
            saw_message_stop = True

    print(f"Assistant: {''.join(text_chunks)}")
    print(f"SSE events: message_start={saw_message_start} message_stop={saw_message_stop}\n")
    return saw_message_start and saw_message_stop and bool(text_chunks)


# ---------------------------------------------------------------------------
# Scenario 4: Messages parallel tool use
# ---------------------------------------------------------------------------


def run_messages_parallel_tools(pat: str) -> bool:
    print(f"=== Scenario: messages parallel tool_use ({MESSAGES_ENDPOINT}) ===")
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 256,
        "tools": [
            {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
        "messages": [
            {"role": "user", "content": "What is the weather in San Francisco and New York?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will check both."},
                    {"type": "tool_use", "id": "tu_sf", "name": "get_weather", "input": {"city": "San Francisco"}},
                    {"type": "tool_use", "id": "tu_ny", "name": "get_weather", "input": {"city": "New York"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_sf", "content": "65F sunny"},
                    {"type": "tool_result", "tool_use_id": "tu_ny", "content": "55F cloudy"},
                ],
            },
        ],
    }
    try:
        resp = requests.post(MESSAGES_ENDPOINT, headers=_auth_headers(pat), json=payload, timeout=60)
    except requests.ConnectionError as exc:
        print(f"Connection failed: {exc}")
        return False

    print(f"Status: {resp.status_code}")
    if _check_html_login(resp) or resp.status_code != 200:
        print(f"Error body: {resp.text[:500]}")
        return False

    data = resp.json()
    blocks = data.get("content") or []
    text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")
    print(f"Assistant: {text}\n")
    return bool(text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


SCENARIOS = {
    "chat": run_chat_completions,
    "messages": run_messages_basic,
    "stream": run_messages_streaming,
    "parallel_tools": run_messages_parallel_tools,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS) + ["all"],
        default="all",
        help="Which scenario to run (default: all)",
    )
    args = parser.parse_args()

    pat = _load_pat()
    print(f"Proxy: {PROXY_BASE_URL}\n")

    selected = list(SCENARIOS.values()) if args.scenario == "all" else [SCENARIOS[args.scenario]]

    failures = 0
    for fn in selected:
        ok = fn(pat)
        if not ok:
            failures += 1
            print(f"  → FAILED: {fn.__name__}\n")

    if failures:
        print(f"{failures} scenario(s) failed.")
        sys.exit(1)
    print("All scenarios passed.")


if __name__ == "__main__":
    main()

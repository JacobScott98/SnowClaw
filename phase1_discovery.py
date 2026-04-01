#!/usr/bin/env python3
"""Phase 1: Cortex REST API Discovery — parallel_tool_calls compatibility testing."""

import json
import sys
import tomllib
from pathlib import Path

import httpx

CONNECTIONS_PATH = Path.home() / ".snowflake" / "connections.toml"
ACCOUNT = "XAB68032"
BASE_URL = f"https://{ACCOUNT}.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a location.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City name"},
            },
            "required": ["location"],
        },
    },
}

MESSAGES = [
    {"role": "user", "content": "What is the weather in San Francisco?"},
]


def load_token() -> str:
    with open(CONNECTIONS_PATH, "rb") as f:
        cfg = tomllib.load(f)
    return cfg["main"]["token"]


def build_payload(
    model: str,
    *,
    tools: bool = True,
    parallel_tool_calls: object = "__OMIT__",
    stream: bool = False,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
) -> dict:
    payload: dict = {
        "model": model,
        "messages": MESSAGES,
    }
    if tools:
        payload["tools"] = [TOOL_DEF]
    if parallel_tool_calls != "__OMIT__":
        payload["parallel_tool_calls"] = parallel_tool_calls
    if stream:
        payload["stream"] = True
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


def send_request(client: httpx.Client, payload: dict, label: str) -> dict:
    print(f"\n{'='*80}")
    print(f"TEST: {label}")
    print(f"{'='*80}")
    print(f"Model: {payload['model']}")
    print(f"Payload keys: {list(payload.keys())}")
    if "parallel_tool_calls" in payload:
        print(f"parallel_tool_calls = {payload['parallel_tool_calls']}")

    try:
        if payload.get("stream"):
            resp = client.post(BASE_URL, json=payload, timeout=60)
            print(f"Status: {resp.status_code}")
            print(f"Response (streaming raw):")
            print(resp.text[:2000])
            return {"status": resp.status_code, "body": resp.text[:2000], "label": label}
        else:
            resp = client.post(BASE_URL, json=payload, timeout=60)
            print(f"Status: {resp.status_code}")
            try:
                body = resp.json()
                print(f"Response:\n{json.dumps(body, indent=2)[:2000]}")
            except Exception:
                body = resp.text
                print(f"Response (text):\n{body[:2000]}")
            return {"status": resp.status_code, "body": body, "label": label}
    except httpx.HTTPError as e:
        print(f"HTTP Error: {e}")
        return {"status": "error", "body": str(e), "label": label}


def main():
    token = load_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    results = []
    with httpx.Client(headers=headers) as client:
        # ── Claude model tests ──────────────────────────────────────────

        # 1. Claude + parallel_tool_calls: true (expected to fail)
        results.append(send_request(
            client,
            build_payload("claude-3-5-sonnet", parallel_tool_calls=True),
            "Claude + parallel_tool_calls=true",
        ))

        # 2. Claude + no parallel_tool_calls param (expected to succeed)
        results.append(send_request(
            client,
            build_payload("claude-3-5-sonnet"),
            "Claude + parallel_tool_calls OMITTED",
        ))

        # 3. Claude + parallel_tool_calls: false (test explicit false)
        results.append(send_request(
            client,
            build_payload("claude-3-5-sonnet", parallel_tool_calls=False),
            "Claude + parallel_tool_calls=false",
        ))

        # ── OpenAI model tests ──────────────────────────────────────────

        # 4. GPT-4.1 + parallel_tool_calls: true (expected to succeed)
        results.append(send_request(
            client,
            build_payload("openai-gpt-4.1", parallel_tool_calls=True),
            "GPT-4.1 + parallel_tool_calls=true",
        ))

        # 5. GPT-4.1 + no parallel_tool_calls param
        results.append(send_request(
            client,
            build_payload("openai-gpt-4.1"),
            "GPT-4.1 + parallel_tool_calls OMITTED",
        ))

        # 6. GPT-4.1 + parallel_tool_calls: false
        results.append(send_request(
            client,
            build_payload("openai-gpt-4.1", parallel_tool_calls=False),
            "GPT-4.1 + parallel_tool_calls=false",
        ))

        # ── Streaming tests ─────────────────────────────────────────────

        # 7. Claude + stream + parallel_tool_calls: true
        results.append(send_request(
            client,
            build_payload("claude-3-5-sonnet", parallel_tool_calls=True, stream=True),
            "Claude + stream + parallel_tool_calls=true",
        ))

        # 8. Claude + stream + no parallel_tool_calls
        results.append(send_request(
            client,
            build_payload("claude-3-5-sonnet", stream=True),
            "Claude + stream + parallel_tool_calls OMITTED",
        ))

        # 9. GPT-4.1 + stream + parallel_tool_calls: true
        results.append(send_request(
            client,
            build_payload("openai-gpt-4.1", parallel_tool_calls=True, stream=True),
            "GPT-4.1 + stream + parallel_tool_calls=true",
        ))

        # ── Parameter probing ───────────────────────────────────────────

        # 10. Claude + temperature
        results.append(send_request(
            client,
            build_payload("claude-3-5-sonnet", tools=False, temperature=0.5),
            "Claude + temperature=0.5 (no tools)",
        ))

        # 11. Claude + max_tokens
        results.append(send_request(
            client,
            build_payload("claude-3-5-sonnet", tools=False, max_tokens=100),
            "Claude + max_tokens=100 (no tools)",
        ))

        # 12. Claude + response_format json_object
        results.append(send_request(
            client,
            build_payload(
                "claude-3-5-sonnet",
                tools=False,
                response_format={"type": "json_object"},
            ),
            "Claude + response_format=json_object (no tools)",
        ))

        # 13. GPT-4.1 + temperature
        results.append(send_request(
            client,
            build_payload("openai-gpt-4.1", tools=False, temperature=0.5),
            "GPT-4.1 + temperature=0.5 (no tools)",
        ))

        # 14. GPT-4.1 + max_tokens
        results.append(send_request(
            client,
            build_payload("openai-gpt-4.1", tools=False, max_tokens=100),
            "GPT-4.1 + max_tokens=100 (no tools)",
        ))

        # 15. GPT-4.1 + response_format json_object
        results.append(send_request(
            client,
            build_payload(
                "openai-gpt-4.1",
                tools=False,
                response_format={"type": "json_object"},
            ),
            "GPT-4.1 + response_format=json_object (no tools)",
        ))

    # ── Summary ─────────────────────────────────────────────────────────
    print(f"\n\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    for r in results:
        status = r["status"]
        marker = "PASS" if status == 200 else "FAIL"
        print(f"  [{marker}] {r['label']}  (HTTP {status})")


if __name__ == "__main__":
    main()

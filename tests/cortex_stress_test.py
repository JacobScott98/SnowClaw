"""Stress tests for the Cortex proxy against a live Snowflake Cortex endpoint.

Run manually with a local proxy:
    1. PROXY_LOG_RESPONSES=1 python proxy/app.py
    2. python tests/cortex_stress_test.py

Reads PAT from SNOWFLAKE_PAT env var or ~/.snowflake/connections.toml.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import httpx

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8080")
MODEL = "claude-sonnet-4-6"
TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def load_pat() -> str:
    """Load Snowflake PAT from env or connections.toml."""
    pat = os.environ.get("SNOWFLAKE_PAT")
    if pat:
        return pat

    toml_path = Path.home() / ".snowflake" / "connections.toml"
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        # Resolve default connection name, then look up that section
        default_name = data.get("default_connection_name", "")
        conn = data.get(default_name) if default_name else None
        if conn is None:
            # Fall back to first dict-valued entry
            conn = next((v for v in data.values() if isinstance(v, dict)), {})
        if isinstance(conn, dict):
            pat = conn.get("token") or conn.get("password") or ""
            if pat:
                return pat

    print("ERROR: No PAT found. Set SNOWFLAKE_PAT or configure ~/.snowflake/connections.toml")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Request helper
# ---------------------------------------------------------------------------


def make_request(
    pat: str,
    messages: list[dict[str, str]],
    *,
    extra_headers: dict[str, str] | None = None,
    stream: bool = True,
    max_completion_tokens: int = 256,
) -> dict[str, Any]:
    """Send a chat completion request to the proxy and return parsed result.

    For streaming: collects all SSE chunks, extracts usage from the final one.
    For non-streaming: returns the JSON response directly.
    """
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    body: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": max_completion_tokens,
        "stream": stream,
    }

    url = f"{PROXY_URL}/v1/chat/completions"

    if not stream:
        resp = httpx.post(url, json=body, headers=headers, timeout=TIMEOUT)
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.json() if resp.status_code == 200 else resp.text,
        }

    # Streaming — collect chunks and extract usage from final data line
    usage = None
    chunks_received = 0
    content_parts: list[str] = []

    with httpx.stream("POST", url, json=body, headers=headers, timeout=TIMEOUT) as resp:
        if resp.status_code != 200:
            error_body = resp.read().decode("utf-8", errors="replace")
            return {
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": error_body,
            }

        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload.strip() == "[DONE]":
                continue
            try:
                parsed = json.loads(payload)
            except (ValueError, TypeError):
                continue

            chunks_received += 1

            # Collect content
            choices = parsed.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    content_parts.append(content)

            # Capture usage (present in final chunk)
            if parsed.get("usage"):
                usage = parsed["usage"]

    return {
        "status_code": 200,
        "chunks_received": chunks_received,
        "content_preview": "".join(content_parts)[:200],
        "usage": usage,
        "headers": dict(resp.headers),
    }


# ---------------------------------------------------------------------------
# Test 1: Caching
# ---------------------------------------------------------------------------


def test_caching(pat: str) -> None:
    """Send identical requests twice to test Cortex prompt caching."""
    print("\n" + "=" * 70)
    print("TEST 1: Caching Behavior")
    print("=" * 70)

    # Large system prompt to trigger caching (>1024 tokens needed for auto-cache)
    # ~4K tokens of repeated text
    filler = "The quick brown fox jumps over the lazy dog. " * 200
    messages = [
        {"role": "system", "content": f"You are a helpful assistant. Context: {filler}"},
        {"role": "user", "content": "Reply with exactly: CACHE_TEST_OK"},
    ]

    print("\nRequest 1 (should create cache)...")
    r1 = make_request(pat, messages)
    print(f"  Status: {r1['status_code']}")
    if r1.get("usage"):
        u = r1["usage"]
        print(f"  Prompt tokens:          {u.get('prompt_tokens', 'N/A')}")
        print(f"  Completion tokens:      {u.get('completion_tokens', 'N/A')}")
        print(f"  Cache creation tokens:  {u.get('cache_creation_input_tokens', 'N/A')}")
        print(f"  Cache read tokens:      {u.get('cache_read_input_tokens', 'N/A')}")
    else:
        print(f"  No usage data returned")
        print(f"  Response: {r1}")

    print("\nRequest 2 (should read from cache)...")
    r2 = make_request(pat, messages)
    print(f"  Status: {r2['status_code']}")
    if r2.get("usage"):
        u = r2["usage"]
        print(f"  Prompt tokens:          {u.get('prompt_tokens', 'N/A')}")
        print(f"  Completion tokens:      {u.get('completion_tokens', 'N/A')}")
        print(f"  Cache creation tokens:  {u.get('cache_creation_input_tokens', 'N/A')}")
        print(f"  Cache read tokens:      {u.get('cache_read_input_tokens', 'N/A')}")
    else:
        print(f"  No usage data returned")
        print(f"  Response: {r2}")

    # Summary
    if r1.get("usage") and r2.get("usage"):
        created = r1["usage"].get("cache_creation_input_tokens", 0) or 0
        read = r2["usage"].get("cache_read_input_tokens", 0) or 0
        if created > 0 and read > 0:
            print("\n  RESULT: Caching is working. Created on first request, read on second.")
        elif created > 0 and read == 0:
            print("\n  RESULT: Cache created but not read on second request. May need longer prompt or TTL issue.")
        elif created == 0 and read == 0:
            print("\n  RESULT: No caching observed. Cortex may not support auto-caching on this endpoint.")
        else:
            print(f"\n  RESULT: Unexpected pattern. created={created}, read={read}")


# ---------------------------------------------------------------------------
# Test 2: Context exhaustion
# ---------------------------------------------------------------------------


def test_context_exhaustion(pat: str) -> None:
    """Send a request exceeding the 200K context window."""
    print("\n" + "=" * 70)
    print("TEST 2: Context Exhaustion (exceed 200K tokens)")
    print("=" * 70)

    # ~250K tokens worth of text (each word is roughly 1 token)
    # "word " = ~1.2 tokens, so 220K repetitions should exceed 200K tokens
    filler = "snowflake " * 220_000
    messages = [
        {"role": "user", "content": f"Summarize this: {filler}"},
    ]

    print(f"\nSending request with ~220K tokens of input...")
    print(f"  Estimated input size: {len(filler) // 4} tokens (rough)")

    result = make_request(pat, messages, max_completion_tokens=64)
    print(f"\n  Status code: {result['status_code']}")

    if result["status_code"] != 200:
        print(f"  Error response body:")
        body = result.get("body", "")
        if isinstance(body, str):
            # Try to parse as JSON for pretty printing
            try:
                parsed = json.loads(body)
                print(f"    {json.dumps(parsed, indent=4)}")
            except (ValueError, TypeError):
                print(f"    {body[:1000]}")
        else:
            print(f"    {body}")

        print(f"\n  Response headers:")
        for k, v in sorted(result.get("headers", {}).items()):
            if k.lower().startswith(("x-", "retry", "content-type")):
                print(f"    {k}: {v}")
    else:
        print(f"  Unexpectedly succeeded!")
        if result.get("usage"):
            print(f"  Usage: {json.dumps(result['usage'], indent=4)}")


# ---------------------------------------------------------------------------
# Test 3: 1M beta header
# ---------------------------------------------------------------------------


def test_beta_1m_context(pat: str) -> None:
    """Test the anthropic-beta header for 1M context window."""
    print("\n" + "=" * 70)
    print("TEST 3: 1M Context Beta Header")
    print("=" * 70)

    # Same oversized payload that failed in test 2, but with the beta header
    # Use ~210K tokens — over the 200K default but under 1M
    filler = "snowflake " * 220_000
    messages = [
        {"role": "user", "content": f"Reply with exactly 'BETA_OK'. Ignore this padding: {filler}"},
    ]

    beta_header = {"anthropic-beta": "context-1m-2025-08-07"}

    print(f"\nSending request with ~220K tokens + anthropic-beta: context-1m-2025-08-07...")

    result = make_request(pat, messages, extra_headers=beta_header, max_completion_tokens=64)
    print(f"\n  Status code: {result['status_code']}")

    if result["status_code"] == 200:
        print(f"  Beta header worked! Request succeeded with >200K tokens.")
        if result.get("usage"):
            u = result["usage"]
            print(f"  Usage:")
            print(f"    Prompt tokens:          {u.get('prompt_tokens', 'N/A')}")
            print(f"    Completion tokens:      {u.get('completion_tokens', 'N/A')}")
            print(f"    Cache creation tokens:  {u.get('cache_creation_input_tokens', 'N/A')}")
            print(f"    Cache read tokens:      {u.get('cache_read_input_tokens', 'N/A')}")
        if result.get("content_preview"):
            print(f"  Content: {result['content_preview']}")
    else:
        print(f"  Beta header did NOT unlock extended context.")
        body = result.get("body", "")
        if isinstance(body, str):
            try:
                parsed = json.loads(body)
                print(f"  Error: {json.dumps(parsed, indent=4)}")
            except (ValueError, TypeError):
                print(f"  Error: {body[:1000]}")
        else:
            print(f"  Error: {body}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    pat = load_pat()
    print(f"Proxy URL: {PROXY_URL}")
    print(f"Model: {MODEL}")
    print(f"PAT loaded: {'*' * 8}...{pat[-8:]}")

    test_caching(pat)
    test_context_exhaustion(pat)
    test_beta_1m_context(pat)

    print("\n" + "=" * 70)
    print("All tests complete. Check proxy logs for Cortex response metadata.")
    print("=" * 70)


if __name__ == "__main__":
    main()

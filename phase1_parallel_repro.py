#!/usr/bin/env python3
"""Phase 1: Reproduce parallel tool call 400 on multi-turn conversation.

Tests the full tool-calling loop:
1. Send request with tools → model returns tool_calls
2. Send follow-up with tool results → does Cortex accept it?

Hypothesis: Claude models on Cortex return 400 when the follow-up message
includes multiple tool-role messages (parallel tool results).
"""

import json
import sys
import tomllib
from pathlib import Path

import httpx

CONNECTIONS_PATH = Path.home() / ".snowflake" / "connections.toml"
ACCOUNT = "XAB68032"
BASE_URL = f"https://{ACCOUNT}.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current time in a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    },
]

# Prompt designed to trigger parallel tool calls
PARALLEL_PROMPT = "What is the weather in San Francisco AND the weather in New York? Call both tools at the same time."


def load_token() -> str:
    with open(CONNECTIONS_PATH, "rb") as f:
        cfg = tomllib.load(f)
    return cfg["main"]["token"]


def send(client: httpx.Client, payload: dict, label: str) -> dict:
    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    print(f"Model: {payload['model']}")
    print(f"Messages ({len(payload['messages'])}):")
    for i, m in enumerate(payload["messages"]):
        role = m["role"]
        if role == "tool":
            print(f"  [{i}] role=tool  tool_call_id={m.get('tool_call_id','?')}  content={m.get('content','')[:80]}")
        elif role == "assistant" and m.get("tool_calls"):
            tc_ids = [tc["id"] for tc in m["tool_calls"]]
            print(f"  [{i}] role=assistant  tool_calls={tc_ids}")
        else:
            content = m.get("content", "")
            print(f"  [{i}] role={role}  content={content[:80]}")

    try:
        resp = client.post(BASE_URL, json=payload, timeout=90)
        print(f"\nStatus: {resp.status_code}")
        try:
            body = resp.json()
            print(f"Response:\n{json.dumps(body, indent=2)[:3000]}")
        except Exception:
            body = resp.text
            print(f"Response (text):\n{body[:3000]}")
        return {"status": resp.status_code, "body": body, "label": label}
    except httpx.HTTPError as e:
        print(f"HTTP Error: {e}")
        return {"status": "error", "body": str(e), "label": label}


def extract_tool_calls(result: dict) -> list[dict] | None:
    """Extract tool_calls from a successful response."""
    if result["status"] != 200:
        return None
    body = result["body"]
    if isinstance(body, str):
        return None
    choices = body.get("choices", [])
    if not choices:
        return None
    msg = choices[0].get("message", {})
    return msg.get("tool_calls")


def fake_tool_result(tool_call: dict) -> str:
    """Generate a fake tool result based on the tool call."""
    fn = tool_call["function"]["name"]
    args = json.loads(tool_call["function"]["arguments"])
    city = args.get("city", args.get("location", "Unknown"))
    if fn == "get_weather":
        return json.dumps({"city": city, "temp_f": 65, "condition": "sunny"})
    elif fn == "get_time":
        return json.dumps({"city": city, "time": "2026-03-30T10:00:00-07:00"})
    return json.dumps({"result": f"ok for {fn}"})


def test_parallel_multi_turn(client: httpx.Client, model: str, label_prefix: str) -> list[dict]:
    """Test the full parallel tool call loop for a model."""
    results = []

    # Step 1: Initial request with tools — trigger parallel tool calls
    payload_1 = {
        "model": model,
        "messages": [{"role": "user", "content": PARALLEL_PROMPT}],
        "tools": TOOLS,
    }
    r1 = send(client, payload_1, f"{label_prefix} — Step 1: Initial request (trigger parallel tool_calls)")
    results.append(r1)

    tool_calls = extract_tool_calls(r1)
    if not tool_calls:
        print(f"\n  ⚠ No tool_calls in response — cannot test multi-turn. Skipping.")
        return results

    print(f"\n  → Extracted {len(tool_calls)} tool_calls: {[tc['id'] for tc in tool_calls]}")

    # Step 2a: Follow-up with ALL tool results at once (parallel)
    assistant_msg = r1["body"]["choices"][0]["message"]
    # Clean the assistant message — keep only role, content, tool_calls
    clean_assistant = {
        "role": "assistant",
        "content": assistant_msg.get("content") or "",
        "tool_calls": assistant_msg["tool_calls"],
    }

    tool_result_msgs = []
    for tc in tool_calls:
        tool_result_msgs.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": fake_tool_result(tc),
        })

    payload_2a = {
        "model": model,
        "messages": [
            {"role": "user", "content": PARALLEL_PROMPT},
            clean_assistant,
            *tool_result_msgs,
        ],
        "tools": TOOLS,
    }
    r2a = send(client, payload_2a, f"{label_prefix} — Step 2a: Follow-up with ALL tool results (parallel)")
    results.append(r2a)

    # Step 2b: Follow-up with tool results ONE AT A TIME (sequential loop)
    # Only test this for Claude since we're investigating Claude-specific issues
    if "claude" in model.lower():
        # Test: send first tool result, get response, then send second
        if len(tool_calls) >= 2:
            # First tool result only
            payload_2b_first = {
                "model": model,
                "messages": [
                    {"role": "user", "content": PARALLEL_PROMPT},
                    clean_assistant,
                    tool_result_msgs[0],
                ],
                "tools": TOOLS,
            }
            r2b = send(client, payload_2b_first,
                       f"{label_prefix} — Step 2b: Follow-up with ONLY FIRST tool result")
            results.append(r2b)

            # All tool results but sent with content as null instead of empty string
            clean_assistant_null = {
                "role": "assistant",
                "content": None,
                "tool_calls": assistant_msg["tool_calls"],
            }
            payload_2c = {
                "model": model,
                "messages": [
                    {"role": "user", "content": PARALLEL_PROMPT},
                    clean_assistant_null,
                    *tool_result_msgs,
                ],
                "tools": TOOLS,
            }
            r2c = send(client, payload_2c,
                       f"{label_prefix} — Step 2c: Follow-up with ALL results (assistant content=null)")
            results.append(r2c)

            # Try without content field on assistant message at all
            clean_assistant_no_content = {
                "role": "assistant",
                "tool_calls": assistant_msg["tool_calls"],
            }
            payload_2d = {
                "model": model,
                "messages": [
                    {"role": "user", "content": PARALLEL_PROMPT},
                    clean_assistant_no_content,
                    *tool_result_msgs,
                ],
                "tools": TOOLS,
            }
            r2d = send(client, payload_2d,
                       f"{label_prefix} — Step 2d: Follow-up with ALL results (no assistant content key)")
            results.append(r2d)

    return results


def test_single_tool_call_multi_turn(client: httpx.Client, model: str, label_prefix: str) -> list[dict]:
    """Control test: single tool call multi-turn (should always work)."""
    results = []

    payload_1 = {
        "model": model,
        "messages": [{"role": "user", "content": "What is the weather in San Francisco?"}],
        "tools": TOOLS,
    }
    r1 = send(client, payload_1, f"{label_prefix} — Single tool: Step 1")
    results.append(r1)

    tool_calls = extract_tool_calls(r1)
    if not tool_calls:
        print(f"\n  ⚠ No tool_calls — skipping.")
        return results

    assistant_msg = r1["body"]["choices"][0]["message"]
    clean_assistant = {
        "role": "assistant",
        "content": assistant_msg.get("content") or "",
        "tool_calls": assistant_msg["tool_calls"],
    }

    payload_2 = {
        "model": model,
        "messages": [
            {"role": "user", "content": "What is the weather in San Francisco?"},
            clean_assistant,
            {
                "role": "tool",
                "tool_call_id": tool_calls[0]["id"],
                "content": fake_tool_result(tool_calls[0]),
            },
        ],
        "tools": TOOLS,
    }
    r2 = send(client, payload_2, f"{label_prefix} — Single tool: Step 2 (follow-up)")
    results.append(r2)

    return results


def main():
    token = load_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    all_results = []
    with httpx.Client(headers=headers) as client:

        # ── Control: Single tool call multi-turn (Claude) ────────────────
        print("\n" + "█" * 80)
        print("  CONTROL TEST: Single tool call multi-turn (should work)")
        print("█" * 80)
        all_results.extend(
            test_single_tool_call_multi_turn(client, "claude-3-5-sonnet", "Claude (control)")
        )

        # ── Main test: Parallel tool calls multi-turn (Claude) ───────────
        print("\n" + "█" * 80)
        print("  MAIN TEST: Parallel tool calls multi-turn (Claude)")
        print("█" * 80)
        all_results.extend(
            test_parallel_multi_turn(client, "claude-3-5-sonnet", "Claude")
        )

        # ── Comparison: Parallel tool calls multi-turn (GPT) ─────────────
        print("\n" + "█" * 80)
        print("  COMPARISON: Parallel tool calls multi-turn (GPT-4.1)")
        print("█" * 80)
        all_results.extend(
            test_parallel_multi_turn(client, "openai-gpt-4.1", "GPT-4.1")
        )

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    for r in all_results:
        status = r["status"]
        if status == 200:
            marker = "✅ PASS"
        elif status == 400:
            marker = "❌ 400 "
        else:
            marker = f"⚠  {status} "
        print(f"  [{marker}] {r['label']}")

        # Print error message for non-200 responses
        if status != 200 and isinstance(r["body"], dict):
            err_msg = r["body"].get("message", r["body"].get("error", ""))
            if err_msg:
                print(f"           Error: {err_msg}")


if __name__ == "__main__":
    main()

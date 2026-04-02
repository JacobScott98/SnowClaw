"""Pure request transformation functions for Cortex compatibility."""

from __future__ import annotations

import copy
from typing import Any

from config import is_claude_model


def transform_request(body: dict[str, Any]) -> dict[str, Any]:
    """Apply all necessary transformations to a chat completions request body."""
    body = copy.deepcopy(body)
    model = body.get("model", "")

    # Universal transforms
    body = rewrite_max_tokens(body)

    # Claude-specific transforms
    if is_claude_model(model):
        body = strip_parallel_tool_calls(body)
        body = strip_response_format(body)
        body = serialize_parallel_tool_calls(body)

    return body


def rewrite_max_tokens(body: dict[str, Any]) -> dict[str, Any]:
    """Rewrite max_tokens → max_completion_tokens for all models.

    Cortex 400s on the deprecated max_tokens parameter.
    """
    if "max_tokens" in body:
        if "max_completion_tokens" not in body:
            body["max_completion_tokens"] = body["max_tokens"]
        del body["max_tokens"]
    return body


def strip_parallel_tool_calls(body: dict[str, Any]) -> dict[str, Any]:
    """Remove parallel_tool_calls from the request body (meaningless for Claude on Cortex)."""
    body.pop("parallel_tool_calls", None)
    return body


def strip_response_format(body: dict[str, Any]) -> dict[str, Any]:
    """Remove response_format if it's json_object (Cortex 400s for Claude)."""
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        del body["response_format"]
    return body


def serialize_parallel_tool_calls(body: dict[str, Any]) -> dict[str, Any]:
    """Rewrite parallel tool_calls into sequential single-tool-call turns.

    When an assistant message has multiple tool_calls, Cortex's translation to
    the Anthropic API fails with "Each toolUse block must be accompanied with a
    matching toolResult block." This transform splits each parallel batch into
    sequential assistant(1 tool_call) → tool(result) pairs.
    """
    messages = body.get("messages")
    if not messages:
        return body

    new_messages: list[dict[str, Any]] = []
    i = 0

    while i < len(messages):
        msg = messages[i]

        # Check for assistant message with multiple tool_calls
        tool_calls = msg.get("tool_calls", [])
        if msg.get("role") == "assistant" and len(tool_calls) > 1:
            # Collect the subsequent tool result messages
            tool_results: dict[str, dict[str, Any]] = {}
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_msg = messages[j]
                call_id = tool_msg.get("tool_call_id")
                if call_id:
                    tool_results[call_id] = tool_msg
                j += 1

            # Emit sequential single-tool-call turns
            for tc in tool_calls:
                # Assistant message with single tool_call
                assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": [tc]}
                # Preserve content if present on first one only
                if tc is tool_calls[0] and msg.get("content"):
                    assistant_msg["content"] = msg["content"]
                new_messages.append(assistant_msg)

                # Matching tool result
                call_id = tc.get("id")
                if call_id and call_id in tool_results:
                    new_messages.append(tool_results[call_id])

            # Skip past the tool result messages we consumed
            i = j
        else:
            new_messages.append(msg)
            i += 1

    body["messages"] = new_messages
    return body

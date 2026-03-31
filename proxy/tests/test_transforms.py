"""Unit tests for request transformation logic."""

import sys
from pathlib import Path

# Add proxy directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transforms import (
    rewrite_max_tokens,
    serialize_parallel_tool_calls,
    strip_parallel_tool_calls,
    strip_response_format,
    transform_request,
)


# --- rewrite_max_tokens ---


def test_rewrite_max_tokens():
    body = {"model": "claude-3-5-sonnet", "max_tokens": 1024}
    result = rewrite_max_tokens(body)
    assert "max_tokens" not in result
    assert result["max_completion_tokens"] == 1024


def test_rewrite_max_tokens_preserves_existing_max_completion_tokens():
    body = {"model": "gpt-4", "max_tokens": 1024, "max_completion_tokens": 2048}
    result = rewrite_max_tokens(body)
    assert "max_tokens" not in result
    assert result["max_completion_tokens"] == 2048


def test_rewrite_max_tokens_noop_without_max_tokens():
    body = {"model": "claude-3-5-sonnet", "max_completion_tokens": 512}
    result = rewrite_max_tokens(body)
    assert result["max_completion_tokens"] == 512
    assert "max_tokens" not in result


# --- strip_parallel_tool_calls ---


def test_strip_parallel_tool_calls():
    body = {"model": "claude-3-5-sonnet", "parallel_tool_calls": True}
    result = strip_parallel_tool_calls(body)
    assert "parallel_tool_calls" not in result


def test_strip_parallel_tool_calls_noop():
    body = {"model": "claude-3-5-sonnet"}
    result = strip_parallel_tool_calls(body)
    assert "parallel_tool_calls" not in result


# --- strip_response_format ---


def test_strip_response_format_json_object():
    body = {"model": "claude-3-5-sonnet", "response_format": {"type": "json_object"}}
    result = strip_response_format(body)
    assert "response_format" not in result


def test_strip_response_format_preserves_other_types():
    body = {"model": "claude-3-5-sonnet", "response_format": {"type": "text"}}
    result = strip_response_format(body)
    assert result["response_format"] == {"type": "text"}


def test_strip_response_format_noop():
    body = {"model": "claude-3-5-sonnet"}
    result = strip_response_format(body)
    assert "response_format" not in result


# --- serialize_parallel_tool_calls ---


def _make_tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _make_tool_result(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_serialize_two_parallel_tool_calls():
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "Weather in SF and NYC?"},
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("1", "weather", '{"loc": "SF"}'),
                    _make_tool_call("2", "weather", '{"loc": "NYC"}'),
                ],
            },
            _make_tool_result("1", "72F sunny"),
            _make_tool_result("2", "65F cloudy"),
        ],
    }
    result = serialize_parallel_tool_calls(body)
    msgs = result["messages"]

    assert len(msgs) == 5  # user + (assistant+tool) * 2
    assert msgs[0]["role"] == "user"

    # First pair
    assert msgs[1]["role"] == "assistant"
    assert len(msgs[1]["tool_calls"]) == 1
    assert msgs[1]["tool_calls"][0]["id"] == "1"
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["tool_call_id"] == "1"
    assert msgs[2]["content"] == "72F sunny"

    # Second pair
    assert msgs[3]["role"] == "assistant"
    assert len(msgs[3]["tool_calls"]) == 1
    assert msgs[3]["tool_calls"][0]["id"] == "2"
    assert msgs[4]["role"] == "tool"
    assert msgs[4]["tool_call_id"] == "2"
    assert msgs[4]["content"] == "65F cloudy"


def test_serialize_three_parallel_tool_calls():
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "Weather in SF, NYC, LA?"},
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("a", "weather", '{"loc": "SF"}'),
                    _make_tool_call("b", "weather", '{"loc": "NYC"}'),
                    _make_tool_call("c", "weather", '{"loc": "LA"}'),
                ],
            },
            _make_tool_result("a", "72F"),
            _make_tool_result("b", "65F"),
            _make_tool_result("c", "80F"),
        ],
    }
    result = serialize_parallel_tool_calls(body)
    msgs = result["messages"]

    assert len(msgs) == 7  # user + 3 pairs
    # Verify ordering: assistant(a), tool(a), assistant(b), tool(b), assistant(c), tool(c)
    for idx, call_id in enumerate(["a", "b", "c"]):
        a_idx = 1 + idx * 2
        t_idx = 2 + idx * 2
        assert msgs[a_idx]["tool_calls"][0]["id"] == call_id
        assert msgs[t_idx]["tool_call_id"] == call_id


def test_serialize_single_tool_call_passthrough():
    """Single tool_call should not be modified."""
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "Weather in SF?"},
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("1", "weather", '{"loc": "SF"}'),
                ],
            },
            _make_tool_result("1", "72F sunny"),
        ],
    }
    result = serialize_parallel_tool_calls(body)
    msgs = result["messages"]

    assert len(msgs) == 3
    assert len(msgs[1]["tool_calls"]) == 1


def test_serialize_nested_in_conversation():
    """Parallel tool calls in the middle of a longer conversation."""
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Weather in SF and NYC?"},
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("1", "weather", '{"loc": "SF"}'),
                    _make_tool_call("2", "weather", '{"loc": "NYC"}'),
                ],
            },
            _make_tool_result("1", "72F"),
            _make_tool_result("2", "65F"),
            {"role": "user", "content": "Thanks!"},
        ],
    }
    result = serialize_parallel_tool_calls(body)
    msgs = result["messages"]

    assert len(msgs) == 8  # Hi, Hello, Weather?, a+t, a+t, Thanks
    assert msgs[0] == {"role": "user", "content": "Hi"}
    assert msgs[1] == {"role": "assistant", "content": "Hello!"}
    assert msgs[2] == {"role": "user", "content": "Weather in SF and NYC?"}
    assert msgs[3]["role"] == "assistant"
    assert len(msgs[3]["tool_calls"]) == 1
    assert msgs[4]["role"] == "tool"
    assert msgs[5]["role"] == "assistant"
    assert msgs[6]["role"] == "tool"
    assert msgs[7] == {"role": "user", "content": "Thanks!"}


def test_serialize_preserves_assistant_content():
    """Content on the assistant message should be preserved on the first split message."""
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "Check weather"},
            {
                "role": "assistant",
                "content": "Let me check both cities.",
                "tool_calls": [
                    _make_tool_call("1", "weather", '{"loc": "SF"}'),
                    _make_tool_call("2", "weather", '{"loc": "NYC"}'),
                ],
            },
            _make_tool_result("1", "72F"),
            _make_tool_result("2", "65F"),
        ],
    }
    result = serialize_parallel_tool_calls(body)
    msgs = result["messages"]

    assert msgs[1]["content"] == "Let me check both cities."
    assert "content" not in msgs[3]  # Second assistant msg has no content


def test_serialize_no_messages():
    body = {"model": "claude-3-5-sonnet"}
    result = serialize_parallel_tool_calls(body)
    assert "messages" not in result


def test_serialize_empty_messages():
    body = {"model": "claude-3-5-sonnet", "messages": []}
    result = serialize_parallel_tool_calls(body)
    assert result["messages"] == []


def test_serialize_no_tool_calls():
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ],
    }
    result = serialize_parallel_tool_calls(body)
    assert len(result["messages"]) == 2


def test_serialize_multiple_parallel_batches():
    """Two separate parallel tool call batches in one conversation."""
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "Weather in SF and NYC?"},
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("1", "weather", '{"loc": "SF"}'),
                    _make_tool_call("2", "weather", '{"loc": "NYC"}'),
                ],
            },
            _make_tool_result("1", "72F"),
            _make_tool_result("2", "65F"),
            {"role": "assistant", "content": "SF is 72F, NYC is 65F."},
            {"role": "user", "content": "Now check LA and Chicago"},
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("3", "weather", '{"loc": "LA"}'),
                    _make_tool_call("4", "weather", '{"loc": "CHI"}'),
                ],
            },
            _make_tool_result("3", "80F"),
            _make_tool_result("4", "55F"),
        ],
    }
    result = serialize_parallel_tool_calls(body)
    msgs = result["messages"]

    # user + (a+t)*2 + assistant + user + (a+t)*2 = 1+4+1+1+4 = 11
    assert len(msgs) == 11


# --- transform_request (integration) ---


def test_transform_request_claude_full():
    body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "parallel_tool_calls": True,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "user", "content": "Weather in SF and NYC?"},
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("1", "weather", '{"loc": "SF"}'),
                    _make_tool_call("2", "weather", '{"loc": "NYC"}'),
                ],
            },
            _make_tool_result("1", "72F"),
            _make_tool_result("2", "65F"),
        ],
    }
    result = transform_request(body)

    assert "max_tokens" not in result
    assert result["max_completion_tokens"] == 1024
    assert "parallel_tool_calls" not in result
    assert "response_format" not in result
    assert len(result["messages"]) == 5  # serialized


def test_transform_request_openai_passthrough():
    body = {
        "model": "openai-gpt-4.1",
        "max_tokens": 1024,
        "parallel_tool_calls": True,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "user", "content": "Weather in SF and NYC?"},
            {
                "role": "assistant",
                "tool_calls": [
                    _make_tool_call("1", "weather", '{"loc": "SF"}'),
                    _make_tool_call("2", "weather", '{"loc": "NYC"}'),
                ],
            },
            _make_tool_result("1", "72F"),
            _make_tool_result("2", "65F"),
        ],
    }
    result = transform_request(body)

    # max_tokens rewritten for all models
    assert "max_tokens" not in result
    assert result["max_completion_tokens"] == 1024

    # OpenAI-specific params preserved
    assert result["parallel_tool_calls"] is True
    assert result["response_format"] == {"type": "json_object"}

    # Messages NOT serialized for OpenAI
    assert len(result["messages"]) == 4


def test_transform_request_does_not_mutate_original():
    body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    original_body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Hi"}],
    }
    transform_request(body)
    assert body == original_body

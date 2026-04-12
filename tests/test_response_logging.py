"""Tests for proxy response metadata logging."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))

from response_logging import (
    _redact_choices,
    extract_response_metadata,
    extract_usage_from_sse_line,
    log_response_metadata,
)


# ---------------------------------------------------------------------------
# _redact_choices
# ---------------------------------------------------------------------------


class TestRedactChoices:
    def test_text_content_redacted(self):
        choices = [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "This is a secret message that should not appear in logs.",
                },
            }
        ]
        result = _redact_choices(choices)
        assert len(result) == 1
        assert result[0]["index"] == 0
        assert result[0]["finish_reason"] == "stop"
        assert "content" not in result[0]["message"]
        assert result[0]["message"]["content_length"] == len(choices[0]["message"]["content"])
        assert result[0]["message"]["role"] == "assistant"

    def test_tool_calls_metadata_preserved(self):
        choices = [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Seattle"}',
                            },
                        }
                    ],
                },
            }
        ]
        result = _redact_choices(choices)
        tc = result[0]["message"]["tool_calls"][0]
        assert tc["id"] == "call_123"
        assert tc["function"]["name"] == "get_weather"
        assert tc["function"]["arguments_length"] == len('{"city": "Seattle"}')
        assert "arguments" not in tc["function"]

    def test_delta_streaming_redacted(self):
        choices = [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": "partial content",
                },
            }
        ]
        result = _redact_choices(choices)
        assert "content" not in result[0]["delta"]
        assert result[0]["delta"]["content_length"] == 15

    def test_refusal_field_redacted(self):
        choices = [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "refusal": "I cannot help with that.",
                },
            }
        ]
        result = _redact_choices(choices)
        assert "refusal" not in result[0]["message"]
        assert result[0]["message"]["refusal_length"] == len("I cannot help with that.")

    def test_empty_content_not_included(self):
        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
            }
        ]
        result = _redact_choices(choices)
        # Empty string is falsy, so content_length should not appear
        assert "content_length" not in result[0]["message"]


# ---------------------------------------------------------------------------
# extract_response_metadata
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int = 200,
    headers: dict | None = None,
    json_body: dict | None = None,
) -> httpx.Response:
    resp = httpx.Response(status_code, headers=headers or {}, json=json_body or {})
    return resp


class TestExtractResponseMetadata:
    def test_basic_fields(self):
        resp = _make_response(200)
        meta = extract_response_metadata(resp, {"id": "chatcmpl-abc", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, "claude-3.5-sonnet")
        assert meta["status_code"] == 200
        assert meta["model"] == "claude-3.5-sonnet"
        assert meta["id"] == "chatcmpl-abc"
        assert meta["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}

    def test_cortex_headers_extracted(self):
        resp = _make_response(200, headers={
            "x-request-id": "req-123",
            "x-ratelimit-remaining": "99",
        })
        meta = extract_response_metadata(resp, {}, "claude")
        assert meta["cortex"]["x-request-id"] == "req-123"
        assert meta["cortex"]["x-ratelimit-remaining"] == "99"

    def test_all_headers_captured(self):
        resp = _make_response(200, headers={"X-Custom": "value"})
        meta = extract_response_metadata(resp, {}, "claude")
        assert "x-custom" in meta["headers"]
        assert meta["headers"]["x-custom"] == "value"

    def test_error_body_included(self):
        resp = _make_response(400)
        body = {"error": {"message": "Invalid request", "type": "invalid_request_error"}}
        meta = extract_response_metadata(resp, body, "claude")
        assert meta["error"] == body["error"]

    def test_retry_count_included(self):
        resp = _make_response(200)
        resp.retry_count = 2  # type: ignore[attr-defined]
        meta = extract_response_metadata(resp, {}, "claude")
        assert meta["retry_count"] == 2

    def test_retry_count_zero_excluded(self):
        resp = _make_response(200)
        resp.retry_count = 0  # type: ignore[attr-defined]
        meta = extract_response_metadata(resp, {}, "claude")
        assert "retry_count" not in meta

    def test_null_body(self):
        resp = _make_response(200)
        meta = extract_response_metadata(resp, None, "claude")
        assert meta["status_code"] == 200
        assert "usage" not in meta

    def test_choices_redacted_in_metadata(self):
        body = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "secret answer"},
                }
            ]
        }
        resp = _make_response(200)
        meta = extract_response_metadata(resp, body, "claude")
        # Content should be redacted
        assert "content" not in meta["choices"][0]["message"]
        assert meta["choices"][0]["message"]["content_length"] == len("secret answer")


# ---------------------------------------------------------------------------
# log_response_metadata
# ---------------------------------------------------------------------------


class TestLogResponseMetadata:
    def test_logs_json_at_info(self, caplog):
        resp = _make_response(200)
        body = {"id": "test-123", "usage": {"prompt_tokens": 5}}
        with caplog.at_level(logging.INFO, logger="response_logging"):
            log_response_metadata(resp, body, "claude")
        assert len(caplog.records) == 1
        assert "Cortex response metadata:" in caplog.records[0].message
        logged = json.loads(caplog.records[0].message.split("Cortex response metadata: ")[1])
        assert logged["id"] == "test-123"
        assert logged["status_code"] == 200


# ---------------------------------------------------------------------------
# Config toggle
# ---------------------------------------------------------------------------


class TestConfigToggle:
    def test_disabled_by_default(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {}, clear=True):
            from config import is_response_logging_enabled
            assert is_response_logging_enabled() is False

    def test_enabled_with_1(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"PROXY_LOG_RESPONSES": "1"}):
            from config import is_response_logging_enabled
            assert is_response_logging_enabled() is True

    def test_enabled_with_true(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"PROXY_LOG_RESPONSES": "true"}):
            from config import is_response_logging_enabled
            assert is_response_logging_enabled() is True

    def test_enabled_with_yes(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"PROXY_LOG_RESPONSES": "YES"}):
            from config import is_response_logging_enabled
            assert is_response_logging_enabled() is True


# ---------------------------------------------------------------------------
# extract_usage_from_sse_line
# ---------------------------------------------------------------------------


class TestExtractUsageFromSseLine:
    def test_returns_chunk_with_usage(self):
        line = 'data: {"id":"chatcmpl-1","usage":{"prompt_tokens":10,"completion_tokens":5}}'
        result = extract_usage_from_sse_line(line)
        assert result is not None
        assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
        assert result["id"] == "chatcmpl-1"

    def test_returns_none_for_chunk_without_usage(self):
        line = 'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"hi"}}]}'
        assert extract_usage_from_sse_line(line) is None

    def test_returns_none_for_empty_usage(self):
        line = 'data: {"id":"chatcmpl-1","usage":{}}'
        assert extract_usage_from_sse_line(line) is None

    def test_returns_none_for_done_sentinel(self):
        assert extract_usage_from_sse_line("data: [DONE]") is None

    def test_returns_none_for_non_data_line(self):
        assert extract_usage_from_sse_line("event: message") is None
        assert extract_usage_from_sse_line("") is None
        assert extract_usage_from_sse_line(": comment") is None

    def test_returns_none_for_malformed_json(self):
        assert extract_usage_from_sse_line("data: {not json}") is None

    def test_cache_stats_in_usage(self):
        line = 'data: {"usage":{"prompt_tokens":100,"completion_tokens":20,"cache_creation_input_tokens":50,"cache_read_input_tokens":30}}'
        result = extract_usage_from_sse_line(line)
        assert result is not None
        assert result["usage"]["cache_creation_input_tokens"] == 50
        assert result["usage"]["cache_read_input_tokens"] == 30

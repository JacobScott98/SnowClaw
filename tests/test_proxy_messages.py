"""Tests for the /v1/messages (Anthropic Messages-shape) proxy endpoint."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

# Add proxy/ to path so we can import the modules directly (matches test_proxy_retry.py pattern).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))

# Set required env BEFORE importing app (config.get_cortex_base_url is called lazily but
# we want predictable behavior across tests).
os.environ.setdefault("CORTEX_BASE_URL", "https://test-account.snowflakecomputing.com/api/v2/cortex/v1")

from app import app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _mock_upstream(status_code: int = 200, json_body: dict | None = None) -> httpx.Response:
    """Build a real httpx.Response that the handler will read with .json()."""
    return httpx.Response(status_code, json=json_body or {"id": "msg_test", "usage": {}})


def _capture_send(json_body: dict | None = None, status_code: int = 200):
    """Build an AsyncMock for send_with_retry that records call args and returns a fake response.

    Returns (mock_send, captured) where captured is a dict populated on call.
    """
    captured: dict = {}

    async def fake(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        resp = _mock_upstream(status_code=status_code, json_body=json_body)
        resp.retry_count = 0  # type: ignore[attr-defined]
        return resp

    return AsyncMock(side_effect=fake), captured


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRoutes:
    def test_v1_messages_path(self, client: TestClient):
        mock_send, _ = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            resp = client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "max_tokens": 16, "messages": []},
            )
        assert resp.status_code == 200
        assert mock_send.await_count == 1

    def test_messages_alias_path(self, client: TestClient):
        """The /messages alias also routes to the same handler."""
        mock_send, _ = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            resp = client.post(
                "/messages",
                json={"model": "claude-sonnet-4-6", "max_tokens": 16, "messages": []},
            )
        assert resp.status_code == 200
        assert mock_send.await_count == 1


# ---------------------------------------------------------------------------
# Upstream URL + headers
# ---------------------------------------------------------------------------


class TestUpstreamCall:
    def test_upstream_url_ends_with_messages(self, client: TestClient):
        mock_send, captured = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "messages": []})
        upstream_url = captured["args"][2]
        assert upstream_url.endswith("/messages")
        assert "/chat/completions" not in upstream_url

    def test_anthropic_version_header_forced(self, client: TestClient):
        """Cortex requires anthropic-version: 2023-06-01 — proxy sets it regardless of client value."""
        mock_send, captured = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "messages": []},
                headers={"anthropic-version": "2023-01-01"},
            )
        headers = captured["kwargs"]["headers"]
        assert headers["anthropic-version"] == "2023-06-01"

    def test_x_snowflake_token_type_header_set(self, client: TestClient):
        mock_send, captured = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "messages": []})
        headers = captured["kwargs"]["headers"]
        assert headers["X-Snowflake-Authorization-Token-Type"] == "PROGRAMMATIC_ACCESS_TOKEN"


# ---------------------------------------------------------------------------
# Auth header translation
# ---------------------------------------------------------------------------


class TestAuthTranslation:
    def test_x_api_key_translated_to_bearer(self, client: TestClient):
        """OpenClaw's anthropic-messages SDK transport sends x-api-key by default."""
        mock_send, captured = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "messages": []},
                headers={"x-api-key": "fake-pat-12345"},
            )
        assert captured["kwargs"]["headers"]["Authorization"] == "Bearer fake-pat-12345"

    def test_x_cortex_token_takes_precedence_over_x_api_key(self, client: TestClient):
        """X-Cortex-Token is the SPCS-safe path and must win when both are present."""
        mock_send, captured = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "messages": []},
                headers={
                    "x-api-key": "should-be-ignored",
                    "X-Cortex-Token": "spcs-token",
                },
            )
        assert captured["kwargs"]["headers"]["Authorization"] == "Bearer spcs-token"

    def test_snowflake_token_authorization_normalized(self, client: TestClient):
        """Legacy 'Snowflake Token=\"<pat>\"' format is normalized to Bearer."""
        mock_send, captured = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "messages": []},
                headers={"Authorization": 'Snowflake Token="legacy-pat"'},
            )
        assert captured["kwargs"]["headers"]["Authorization"] == "Bearer legacy-pat"

    def test_no_auth_headers_results_in_no_authorization(self, client: TestClient):
        """When no auth headers are sent, the proxy forwards without Authorization (Cortex will reject)."""
        mock_send, captured = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "messages": []},
            )
        assert "Authorization" not in captured["kwargs"]["headers"]


# ---------------------------------------------------------------------------
# Body / transforms
# ---------------------------------------------------------------------------


class TestBody:
    def test_max_tokens_not_rewritten(self, client: TestClient):
        """Regression guard — Anthropic Messages uses max_tokens natively, do NOT rewrite to max_completion_tokens."""
        mock_send, captured = _capture_send()
        with patch("app.send_with_retry", new=mock_send):
            client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "max_tokens": 1024, "messages": []},
            )
        sent_body = captured["kwargs"]["json"]
        assert sent_body["max_tokens"] == 1024
        assert "max_completion_tokens" not in sent_body

    def test_cache_control_passes_through_unchanged(self, client: TestClient):
        """OpenClaw injects cache_control markers — proxy must not strip or alter them."""
        mock_send, captured = _capture_send()
        body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16,
            "system": [
                {"type": "text", "text": "You are an agent.", "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        with patch("app.send_with_retry", new=mock_send):
            client.post("/v1/messages", json=body)
        sent_body = captured["kwargs"]["json"]
        assert sent_body["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_tool_use_blocks_pass_through(self, client: TestClient):
        """Parallel tool_use blocks should reach upstream unchanged (no serialize transform)."""
        mock_send, captured = _capture_send()
        body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "f", "input": {"x": 1}},
                        {"type": "tool_use", "id": "tu_2", "name": "f", "input": {"x": 2}},
                    ],
                },
            ],
        }
        with patch("app.send_with_retry", new=mock_send):
            client.post("/v1/messages", json=body)
        sent = captured["kwargs"]["json"]
        assert len(sent["messages"][0]["content"]) == 2
        assert sent["messages"][0]["content"][0]["id"] == "tu_1"
        assert sent["messages"][0]["content"][1]["id"] == "tu_2"

    def test_masking_applied_to_messages_body(self, client: TestClient):
        """Secrets in the request body should be masked before being forwarded upstream."""
        with patch.dict(os.environ, {"SNOWCLAW_MASK_VARS": "TEST_SECRET", "TEST_SECRET": "supersecretvalue"}):
            # Re-import the masker so it picks up the new env var.
            from masking import SecretMasker
            with patch("app._masker", new=SecretMasker()):
                mock_send, captured = _capture_send()
                with patch("app.send_with_retry", new=mock_send):
                    client.post(
                        "/v1/messages",
                        json={
                            "model": "claude-sonnet-4-6",
                            "messages": [{"role": "user", "content": "use supersecretvalue"}],
                        },
                    )
                sent = captured["kwargs"]["json"]
                assert "supersecretvalue" not in sent["messages"][0]["content"]
                assert "[REDACTED:TEST_SECRET]" in sent["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


class TestResponses:
    def test_non_streaming_returns_upstream_body(self, client: TestClient):
        upstream_body = {
            "id": "msg_bdrk_xyz",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 0},
        }
        mock_send, _ = _capture_send(json_body=upstream_body)
        with patch("app.send_with_retry", new=mock_send):
            resp = client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "messages": []},
            )
        assert resp.status_code == 200
        assert resp.json() == upstream_body

    def test_upstream_4xx_returned_as_error_payload(self, client: TestClient):
        """Non-streaming upstream errors are returned as JSONResponse with the upstream body."""
        mock_send, _ = _capture_send(
            status_code=400, json_body={"error": "bad request"}
        )
        with patch("app.send_with_retry", new=mock_send):
            resp = client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "messages": []},
            )
        # The handler returns the upstream JSON body verbatim with the upstream status code.
        assert resp.status_code == 400
        assert resp.json() == {"error": "bad request"}

    def test_cache_stats_logged_when_present(self, client: TestClient, caplog):
        upstream_body = {
            "id": "msg_x",
            "content": [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 4523,
            },
        }
        mock_send, _ = _capture_send(json_body=upstream_body)
        with caplog.at_level(logging.INFO, logger="app"):
            with patch("app.send_with_retry", new=mock_send):
                client.post(
                    "/v1/messages",
                    json={"model": "claude-sonnet-4-6", "messages": []},
                )
        assert any("Cache stats" in r.message and "read=4523" in r.message for r in caplog.records)

    def test_cache_stats_silent_when_zero(self, client: TestClient, caplog):
        upstream_body = {
            "id": "msg_x",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }
        mock_send, _ = _capture_send(json_body=upstream_body)
        with caplog.at_level(logging.INFO, logger="app"):
            with patch("app.send_with_retry", new=mock_send):
                client.post(
                    "/v1/messages",
                    json={"model": "claude-sonnet-4-6", "messages": []},
                )
        assert not any("Cache stats" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_streaming_passes_bytes_through(self, client: TestClient):
        """Streaming branch must pass raw upstream bytes through unchanged."""
        sse_chunks = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

        class FakeStreamResponse:
            status_code = 200
            retry_count = 0

            async def aiter_raw(self):
                for chunk in sse_chunks:
                    yield chunk

            async def aread(self):
                return b""

            async def aclose(self):
                pass

        async def fake_send(*args, **kwargs):
            return FakeStreamResponse()

        with patch("app.send_with_retry", new=AsyncMock(side_effect=fake_send)):
            with client.stream(
                "POST",
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "stream": True, "messages": []},
            ) as resp:
                assert resp.status_code == 200
                received = b"".join(resp.iter_raw())

        for chunk in sse_chunks:
            assert chunk in received

    def test_streaming_upstream_error_returns_json(self, client: TestClient):
        """Streaming with non-200 upstream returns a JSONResponse with the error body."""

        class FakeErrResponse:
            status_code = 400
            retry_count = 0

            async def aread(self):
                return b'{"error":"bad"}'

            async def aclose(self):
                pass

        async def fake_send(*args, **kwargs):
            return FakeErrResponse()

        with patch("app.send_with_retry", new=AsyncMock(side_effect=fake_send)):
            resp = client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "stream": True, "messages": []},
            )
        assert resp.status_code == 400
        assert "bad" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Connection failures
# ---------------------------------------------------------------------------


class TestConnectFailure:
    def test_connect_error_returns_502(self, client: TestClient):
        async def raise_connect(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        with patch("app.send_with_retry", new=AsyncMock(side_effect=raise_connect)):
            resp = client.post(
                "/v1/messages",
                json={"model": "claude-sonnet-4-6", "messages": []},
            )
        assert resp.status_code == 502
        assert "Cannot reach Cortex" in resp.json()["error"]

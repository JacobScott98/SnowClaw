"""Tests for proxy rate limit retry logic."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

# Add proxy/ to path so we can import the modules directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))

from retry import _get_wait_time, get_base_delay, get_max_retries, send_with_retry


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_max_retries() == 3
            assert get_base_delay() == 1.0

    def test_env_override(self):
        with patch.dict("os.environ", {"PROXY_MAX_RETRIES": "5", "PROXY_RETRY_BASE_DELAY": "0.5"}):
            assert get_max_retries() == 5
            assert get_base_delay() == 0.5


# ---------------------------------------------------------------------------
# Wait time calculation
# ---------------------------------------------------------------------------


class TestGetWaitTime:
    def _resp(self, retry_after: str | None = None) -> httpx.Response:
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return httpx.Response(429, headers=headers)

    def test_exponential_backoff(self):
        resp = self._resp()
        assert _get_wait_time(resp, 0, 1.0) == 1.0
        assert _get_wait_time(resp, 1, 1.0) == 2.0
        assert _get_wait_time(resp, 2, 1.0) == 4.0

    def test_custom_base_delay(self):
        resp = self._resp()
        assert _get_wait_time(resp, 0, 0.5) == 0.5
        assert _get_wait_time(resp, 1, 0.5) == 1.0

    def test_retry_after_header(self):
        resp = self._resp("10")
        assert _get_wait_time(resp, 0, 1.0) == 10.0

    def test_retry_after_invalid_falls_back(self):
        resp = self._resp("not-a-number")
        assert _get_wait_time(resp, 1, 1.0) == 2.0


# ---------------------------------------------------------------------------
# send_with_retry — non-streaming
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, json_body: dict | None = None, headers: dict | None = None):
    """Create a mock httpx.Response."""
    resp = httpx.Response(
        status_code,
        json=json_body or {},
        headers=headers or {},
    )
    return resp


class TestSendWithRetryNonStreaming:
    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request = AsyncMock(return_value=_mock_response(200, {"ok": True}))

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=False, model="claude",
            )

        assert resp.status_code == 200
        assert resp.retry_count == 0  # type: ignore[attr-defined]
        assert mock_client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_then_success(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request = AsyncMock(side_effect=[
            _mock_response(429, {"error": "rate limited"}),
            _mock_response(200, {"ok": True}),
        ])

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=False, model="claude",
            )

        assert resp.status_code == 200
        assert resp.retry_count == 1  # type: ignore[attr-defined]
        assert mock_client.request.call_count == 2

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        rate_limit_resp = _mock_response(429, {"error": "rate limited"})
        mock_client.request = AsyncMock(return_value=rate_limit_resp)

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=False, model="claude",
            )

        assert resp.status_code == 429
        assert resp.retry_count == 3  # type: ignore[attr-defined]
        # 1 initial + 3 retries = 4 calls
        assert mock_client.request.call_count == 4

    @pytest.mark.asyncio
    async def test_non_429_error_no_retry(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request = AsyncMock(return_value=_mock_response(500, {"error": "server error"}))

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=False, model="claude",
            )

        assert resp.status_code == 500
        assert resp.retry_count == 0  # type: ignore[attr-defined]
        assert mock_client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_with_retry_after_header(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request = AsyncMock(side_effect=[
            _mock_response(429, {"error": "rate limited"}, {"Retry-After": "0.01"}),
            _mock_response(200, {"ok": True}),
        ])

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=False, model="claude",
            )

        assert resp.status_code == 200
        assert resp.retry_count == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# send_with_retry — streaming
# ---------------------------------------------------------------------------


def _mock_stream_response(status_code: int, body: bytes = b"", headers: dict | None = None):
    """Create a mock streaming response."""
    resp = httpx.Response(status_code, headers=headers or {})
    resp._content = body  # pre-load content for aread()
    return resp


class TestSendWithRetryStreaming:
    @pytest.mark.asyncio
    async def test_stream_success_no_retry(self):
        mock_resp = _mock_stream_response(200)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.build_request = lambda *a, **kw: httpx.Request("POST", "http://test")
        mock_client.send = AsyncMock(return_value=mock_resp)

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=True, model="claude",
            )

        assert resp.status_code == 200
        assert resp.retry_count == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stream_retry_then_success(self):
        rate_resp = _mock_stream_response(429, b'{"error":"rate limited"}')
        ok_resp = _mock_stream_response(200)

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.build_request = lambda *a, **kw: httpx.Request("POST", "http://test")
        mock_client.send = AsyncMock(side_effect=[rate_resp, ok_resp])

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=True, model="claude",
            )

        assert resp.status_code == 200
        assert resp.retry_count == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stream_max_retries_exhausted(self):
        rate_resp = _mock_stream_response(429, b'{"error":"rate limited"}')

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.build_request = lambda *a, **kw: httpx.Request("POST", "http://test")
        mock_client.send = AsyncMock(return_value=rate_resp)

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=True, model="claude",
            )

        assert resp.status_code == 429
        assert resp.retry_count == 3  # type: ignore[attr-defined]
        # 1 initial + 3 retries = 4 calls
        assert mock_client.send.call_count == 4

    @pytest.mark.asyncio
    async def test_stream_non_429_no_retry(self):
        error_resp = _mock_stream_response(500, b'{"error":"server error"}')

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.build_request = lambda *a, **kw: httpx.Request("POST", "http://test")
        mock_client.send = AsyncMock(return_value=error_resp)

        with patch("retry.get_max_retries", return_value=3), \
             patch("retry.get_base_delay", return_value=0.01):
            resp = await send_with_retry(
                mock_client, "POST", "http://test/v1/chat/completions",
                json={"model": "claude"}, headers={}, stream=True, model="claude",
            )

        assert resp.status_code == 500
        assert resp.retry_count == 0  # type: ignore[attr-defined]
        assert mock_client.send.call_count == 1

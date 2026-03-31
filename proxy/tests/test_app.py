"""Integration tests for the FastAPI proxy app."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set env before importing app
import os

os.environ["CORTEX_BASE_URL"] = "https://test-account.snowflakecomputing.com/api/v2/cortex/v1"

from app import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def _mock_response(status_code=200, json_data=None):
    """Create a mock httpx.Response."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_data or {},
        request=httpx.Request("POST", "https://fake"),
    )
    return resp


class TestNonStreaming:
    def test_basic_request(self, client):
        response_data = {
            "id": "chatcmpl-123",
            "choices": [
                {"message": {"role": "assistant", "content": "Hello!"}, "index": 0}
            ],
        }

        with patch("app._get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                return_value=_mock_response(200, response_data)
            )
            mock_get_client.return_value = mock_client

            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-3-5-sonnet",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers={"Authorization": "Bearer test-token"},
            )

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Hello!"

        # Verify the request was transformed (no max_tokens issues since none sent)
        call_kwargs = mock_client.post.call_args
        sent_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert sent_body["model"] == "claude-3-5-sonnet"

    def test_auth_header_passthrough(self, client):
        with patch("app._get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=_mock_response(200, {}))
            mock_get_client.return_value = mock_client

            client.post(
                "/v1/chat/completions",
                json={"model": "claude-3-5-sonnet", "messages": []},
                headers={"Authorization": "Bearer my-secret-token"},
            )

            call_kwargs = mock_client.post.call_args
            sent_headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get(
                "headers"
            )
            assert sent_headers["Authorization"] == "Bearer my-secret-token"

    def test_max_tokens_rewritten(self, client):
        with patch("app._get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=_mock_response(200, {}))
            mock_get_client.return_value = mock_client

            client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-3-5-sonnet",
                    "max_tokens": 512,
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

            call_kwargs = mock_client.post.call_args
            sent_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert "max_tokens" not in sent_body
            assert sent_body["max_completion_tokens"] == 512

    def test_cortex_error_passthrough(self, client):
        error_data = {"error": {"message": "bad request", "code": 400}}

        with patch("app._get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(
                return_value=_mock_response(400, error_data)
            )
            mock_get_client.return_value = mock_client

            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-3-5-sonnet",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert resp.status_code == 400

    def test_upstream_url(self, client):
        with patch("app._get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=_mock_response(200, {}))
            mock_get_client.return_value = mock_client

            client.post(
                "/v1/chat/completions",
                json={"model": "claude-3-5-sonnet", "messages": []},
            )

            call_args = mock_client.post.call_args
            url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url")
            assert (
                url
                == "https://test-account.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"
            )


class TestStreaming:
    def test_streaming_sse_passthrough(self, client):
        sse_chunks = [
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]

        async def mock_aiter_bytes():
            for chunk in sse_chunks:
                yield chunk

        mock_stream_resp = AsyncMock()
        mock_stream_resp.status_code = 200
        mock_stream_resp.aiter_bytes = mock_aiter_bytes
        mock_stream_resp.aclose = AsyncMock()

        with patch("app._get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.build_request = lambda method, url, **kwargs: httpx.Request(
                method, url
            )
            mock_client.send = AsyncMock(return_value=mock_stream_resp)
            mock_get_client.return_value = mock_client

            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-3-5-sonnet",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Hello" in body
        assert " world" in body
        assert "[DONE]" in body

    def test_streaming_error(self, client):
        mock_stream_resp = AsyncMock()
        mock_stream_resp.status_code = 500
        mock_stream_resp.aread = AsyncMock(return_value=b'{"error": "internal"}')
        mock_stream_resp.aclose = AsyncMock()

        with patch("app._get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.build_request = lambda method, url, **kwargs: httpx.Request(
                method, url
            )
            mock_client.send = AsyncMock(return_value=mock_stream_resp)
            mock_get_client.return_value = mock_client

            resp = client.post(
                "/v1/chat/completions",
                json={
                    "model": "claude-3-5-sonnet",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert resp.status_code == 500

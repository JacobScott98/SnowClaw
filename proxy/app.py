"""FastAPI proxy sidecar for Snowflake Cortex Chat Completions API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from config import get_cortex_base_url
from masking import SecretMasker
from retry import send_with_retry
from transforms import transform_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Lazy-initialized async client
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


app = FastAPI(title="Cortex Proxy", lifespan=lifespan)
_masker = SecretMasker()


def _log_cache_stats(model: str, payload: dict | None) -> None:
    """Log cache hit/miss stats from a non-streaming response body, if present."""
    if not isinstance(payload, dict):
        return
    usage = payload.get("usage") or {}
    created = usage.get("cache_creation_input_tokens", 0) or 0
    read = usage.get("cache_read_input_tokens", 0) or 0
    if created or read:
        logger.info("Cache stats model=%s created=%d read=%d", model, created, read)


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> Response:
    body = await request.json()
    model = body.get("model", "unknown")

    # Mask secrets, then transform the request
    body = _masker.mask_request(body)
    transformed = transform_request(body)
    logger.info("Proxying request for model=%s", model)

    # Build upstream URL and headers
    base_url = get_cortex_base_url()
    upstream_url = f"{base_url}/chat/completions"

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
    }

    # Prefer X-Cortex-Token header (survives SPCS ingress stripping),
    # then fall back to Authorization header (works in local/sidecar mode).
    cortex_token = request.headers.get("X-Cortex-Token")
    if cortex_token:
        headers["Authorization"] = f"Bearer {cortex_token}"
    else:
        auth = request.headers.get("Authorization")
        if auth:
            # Normalize 'Snowflake Token="<pat>"' → 'Bearer <pat>' for Cortex
            if auth.startswith("Snowflake Token="):
                token = auth.split("=", 1)[1].strip('" ')
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers["Authorization"] = auth

    is_streaming = transformed.get("stream", False)
    client = _get_client()

    try:
        if is_streaming:
            upstream_resp = await send_with_retry(
                client, "POST", upstream_url,
                json=transformed, headers=headers, stream=True, model=model,
            )
            retry_count: int = getattr(upstream_resp, "retry_count", 0)

            if upstream_resp.status_code != 200:
                error_body = await upstream_resp.aread()
                await upstream_resp.aclose()
                resp_headers: dict[str, str] = {}
                if retry_count > 0:
                    resp_headers["X-Retry-Count"] = str(retry_count)
                return JSONResponse(
                    status_code=upstream_resp.status_code,
                    content={"error": error_body.decode("utf-8", errors="replace")},
                    headers=resp_headers,
                )

            async def stream_chunks():
                try:
                    async for chunk in upstream_resp.aiter_raw():
                        yield chunk
                finally:
                    await upstream_resp.aclose()

            resp_headers = {
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
            if retry_count > 0:
                resp_headers["X-Retry-Count"] = str(retry_count)

            return StreamingResponse(
                stream_chunks(),
                status_code=upstream_resp.status_code,
                media_type="text/event-stream",
                headers=resp_headers,
            )
        else:
            resp = await send_with_retry(
                client, "POST", upstream_url,
                json=transformed, headers=headers, stream=False, model=model,
            )
            retry_count = getattr(resp, "retry_count", 0)
            resp_headers = {}
            if retry_count > 0:
                resp_headers["X-Retry-Count"] = str(retry_count)
            payload = resp.json()
            _log_cache_stats(model, payload)
            return JSONResponse(
                status_code=resp.status_code,
                content=payload,
                headers=resp_headers,
            )
    except httpx.ConnectError as exc:
        logger.error("Failed to connect to upstream %s: %s", upstream_url, exc)
        return JSONResponse(
            status_code=502,
            content={"error": f"Cannot reach Cortex endpoint: {exc}"},
        )


@app.post("/v1/messages", response_model=None)
@app.post("/messages", response_model=None)
async def messages(request: Request) -> Response:
    """Anthropic Messages-shape proxy for Claude models on Cortex.

    OpenClaw's `anthropic-messages` provider transport speaks this shape and auto-injects
    `cache_control: {type: ephemeral}` markers on system + trailing user blocks. We pass
    those markers through unchanged so Cortex's native /messages endpoint honors them.

    No request transforms here — Anthropic's shape is what Cortex (and Claude) want
    natively. The OpenAI-shape transforms (max_tokens rewrite, parallel tool serialization,
    response_format strip) are deliberately skipped.
    """
    body = await request.json()
    model = body.get("model", "unknown")

    body = _masker.mask_messages_request(body)
    logger.info("Proxying messages request for model=%s", model)

    base_url = get_cortex_base_url()
    upstream_url = f"{base_url.rstrip('/')}/messages"

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        # Cortex's Messages endpoint requires this exact version header. Force-set so we
        # don't depend on the client to send it correctly.
        "anthropic-version": "2023-06-01",
    }

    # Auth priority for the Messages endpoint:
    #   1. X-Cortex-Token (SPCS standalone-proxy mode — survives ingress stripping)
    #   2. x-api-key (what OpenClaw's anthropic-messages SDK transport sends by default)
    #   3. Authorization (local/sidecar mode, Bearer or Snowflake Token shape)
    cortex_token = request.headers.get("X-Cortex-Token")
    if cortex_token:
        headers["Authorization"] = f"Bearer {cortex_token}"
    else:
        api_key = request.headers.get("x-api-key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            auth = request.headers.get("Authorization")
            if auth:
                if auth.startswith("Snowflake Token="):
                    token = auth.split("=", 1)[1].strip('" ')
                    headers["Authorization"] = f"Bearer {token}"
                else:
                    headers["Authorization"] = auth

    is_streaming = body.get("stream", False)
    client = _get_client()

    try:
        if is_streaming:
            upstream_resp = await send_with_retry(
                client, "POST", upstream_url,
                json=body, headers=headers, stream=True, model=model,
            )
            retry_count: int = getattr(upstream_resp, "retry_count", 0)

            if upstream_resp.status_code != 200:
                error_body = await upstream_resp.aread()
                await upstream_resp.aclose()
                resp_headers: dict[str, str] = {}
                if retry_count > 0:
                    resp_headers["X-Retry-Count"] = str(retry_count)
                return JSONResponse(
                    status_code=upstream_resp.status_code,
                    content={"error": error_body.decode("utf-8", errors="replace")},
                    headers=resp_headers,
                )

            async def stream_chunks():
                try:
                    async for chunk in upstream_resp.aiter_raw():
                        yield chunk
                finally:
                    await upstream_resp.aclose()

            resp_headers = {
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
            if retry_count > 0:
                resp_headers["X-Retry-Count"] = str(retry_count)

            return StreamingResponse(
                stream_chunks(),
                status_code=upstream_resp.status_code,
                media_type="text/event-stream",
                headers=resp_headers,
            )
        else:
            resp = await send_with_retry(
                client, "POST", upstream_url,
                json=body, headers=headers, stream=False, model=model,
            )
            retry_count = getattr(resp, "retry_count", 0)
            resp_headers = {}
            if retry_count > 0:
                resp_headers["X-Retry-Count"] = str(retry_count)
            payload = resp.json()
            _log_cache_stats(model, payload)
            return JSONResponse(
                status_code=resp.status_code,
                content=payload,
                headers=resp_headers,
            )
    except httpx.ConnectError as exc:
        logger.error("Failed to connect to upstream %s: %s", upstream_url, exc)
        return JSONResponse(
            status_code=502,
            content={"error": f"Cannot reach Cortex endpoint: {exc}"},
        )


if __name__ == "__main__":
    import uvicorn

    from config import get_proxy_port

    uvicorn.run(app, host="0.0.0.0", port=get_proxy_port())

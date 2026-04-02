"""FastAPI proxy sidecar for Snowflake Cortex Chat Completions API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from config import get_cortex_base_url
from masking import SecretMasker
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

    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth

    is_streaming = transformed.get("stream", False)
    client = _get_client()

    try:
        if is_streaming:
            upstream_req = client.build_request(
                "POST", upstream_url, json=transformed, headers=headers
            )
            upstream_resp = await client.send(upstream_req, stream=True)

            if upstream_resp.status_code != 200:
                error_body = await upstream_resp.aread()
                await upstream_resp.aclose()
                return JSONResponse(
                    status_code=upstream_resp.status_code,
                    content={"error": error_body.decode("utf-8", errors="replace")},
                )

            async def stream_chunks():
                try:
                    async for chunk in upstream_resp.aiter_raw():
                        yield chunk
                finally:
                    await upstream_resp.aclose()

            return StreamingResponse(
                stream_chunks(),
                status_code=upstream_resp.status_code,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
        else:
            resp = await client.post(upstream_url, json=transformed, headers=headers)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
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

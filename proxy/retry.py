"""Retry logic for rate-limited (429) responses from Cortex."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def get_max_retries() -> int:
    return int(os.environ.get("PROXY_MAX_RETRIES", "3"))


def get_base_delay() -> float:
    return float(os.environ.get("PROXY_RETRY_BASE_DELAY", "1.0"))


def _get_wait_time(response: httpx.Response, attempt: int, base_delay: float) -> float:
    """Return wait time from Retry-After header or exponential backoff."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except (ValueError, TypeError):
            pass
    return base_delay * (2 ** attempt)


async def send_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    stream: bool = False,
    model: str = "unknown",
) -> httpx.Response:
    """Send an HTTP request with retry on 429 responses.

    For streaming requests, retries only happen before the stream starts
    (i.e. when the initial response is 429).

    Returns the final httpx.Response. For streaming, the caller is responsible
    for reading/closing the response.
    """
    max_retries = get_max_retries()
    base_delay = get_base_delay()
    attempt = 0

    while True:
        if stream:
            req = client.build_request(method, url, json=json, headers=headers)
            resp = await client.send(req, stream=True)

            if resp.status_code != 429 or attempt >= max_retries:
                resp.retry_count = attempt  # type: ignore[attr-defined]
                return resp

            # Must read and close before retrying
            await resp.aread()
            await resp.aclose()
        else:
            resp = await client.request(method, url, json=json, headers=headers)

            if resp.status_code != 429 or attempt >= max_retries:
                resp.retry_count = attempt  # type: ignore[attr-defined]
                return resp

        wait = _get_wait_time(resp, attempt, base_delay)
        attempt += 1
        logger.warning(
            "Rate limited by Cortex (429). model=%s retry=%d/%d wait=%.1fs",
            model, attempt, max_retries, wait,
        )
        await asyncio.sleep(wait)

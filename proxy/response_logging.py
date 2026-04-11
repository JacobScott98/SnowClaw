"""Log Cortex API response metadata without exposing message content or PII."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Keys in the response body that may contain message content — excluded from logs.
_CONTENT_KEYS = {"content", "refusal"}


def _redact_choices(choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return choices with message/delta content replaced by a length marker."""
    redacted = []
    for choice in choices:
        entry: dict[str, Any] = {"index": choice.get("index")}
        if "finish_reason" in choice:
            entry["finish_reason"] = choice["finish_reason"]

        for msg_key in ("message", "delta"):
            msg = choice.get(msg_key)
            if not msg:
                continue
            safe: dict[str, Any] = {"role": msg.get("role")}
            # Preserve tool_calls metadata (function name/id) but redact arguments
            if "tool_calls" in msg:
                safe["tool_calls"] = [
                    {
                        "id": tc.get("id"),
                        "type": tc.get("type"),
                        "function": {
                            "name": tc.get("function", {}).get("name"),
                            "arguments_length": len(tc.get("function", {}).get("arguments", "")),
                        },
                    }
                    for tc in msg["tool_calls"]
                ]
            for key in _CONTENT_KEYS:
                if key in msg and msg[key]:
                    safe[f"{key}_length"] = len(msg[key])
            entry[msg_key] = safe
        redacted.append(entry)
    return redacted


def extract_response_metadata(
    resp: httpx.Response,
    body: dict[str, Any] | None,
    model: str,
) -> dict[str, Any]:
    """Build a metadata dict from an httpx response suitable for logging.

    Includes HTTP status, headers, Cortex-specific fields, usage stats,
    and error details.  Message content is excluded.
    """
    meta: dict[str, Any] = {
        "model": model,
        "status_code": resp.status_code,
    }

    # Capture all response headers (lowercased keys for consistency).
    meta["headers"] = {k.lower(): v for k, v in resp.headers.items()}

    # Cortex / platform metadata often carried in headers.
    for hdr in (
        "x-request-id",
        "x-snowflake-request-id",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "retry-after",
    ):
        val = resp.headers.get(hdr)
        if val is not None:
            meta.setdefault("cortex", {})[hdr] = val

    retry_count: int = getattr(resp, "retry_count", 0)
    if retry_count:
        meta["retry_count"] = retry_count

    if body is None:
        return meta

    # Usage / token accounting
    if "usage" in body:
        meta["usage"] = body["usage"]

    # Top-level id / created / model echoed back by Cortex
    for key in ("id", "created", "object", "system_fingerprint"):
        if key in body:
            meta[key] = body[key]

    # Redacted choices summary
    if "choices" in body:
        meta["choices"] = _redact_choices(body["choices"])

    # Error details (non-200 responses)
    if "error" in body:
        meta["error"] = body["error"]

    return meta


def log_response_metadata(
    resp: httpx.Response,
    body: dict[str, Any] | None,
    model: str,
) -> None:
    """Log response metadata at INFO level as a single JSON line."""
    meta = extract_response_metadata(resp, body, model)
    logger.info("Cortex response metadata: %s", json.dumps(meta, default=str))

"""Secret masking middleware for the Cortex proxy."""

from __future__ import annotations

import copy
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class SecretMasker:
    """Masks secret values in chat completion request bodies."""

    def __init__(self) -> None:
        mask_vars = os.environ.get("SNOWCLAW_MASK_VARS", "")
        self._secrets: list[tuple[str, str]] = []  # (value, var_name)

        if not mask_vars.strip():
            return

        for var_name in mask_vars.split(","):
            var_name = var_name.strip()
            if not var_name:
                continue
            value = os.environ.get(var_name)
            if not value or len(value) <= 3:
                continue
            self._secrets.append((value, var_name))

        # Sort longest-first for greedy matching
        self._secrets.sort(key=lambda s: len(s[0]), reverse=True)

    def mask_string(self, text: str) -> tuple[str, list[str]]:
        """Replace all secret values in text with [REDACTED:VAR_NAME].

        Returns (masked_text, list_of_redacted_var_names).
        """
        redacted: list[str] = []
        for value, var_name in self._secrets:
            if value in text:
                text = text.replace(value, f"[REDACTED:{var_name}]")
                redacted.append(var_name)
        return text, redacted

    def mask_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Deep scan and mask secrets in a chat completions request body.

        Returns a masked copy — does NOT mutate the original.
        """
        if not self._secrets:
            return body

        body = copy.deepcopy(body)
        all_redacted: list[str] = []

        messages = body.get("messages")
        if messages:
            for msg in messages:
                redacted = self._mask_message(msg)
                all_redacted.extend(redacted)

        if all_redacted:
            unique = list(dict.fromkeys(all_redacted))  # preserve order, dedupe
            logger.info("Masked secrets in request: %s", ", ".join(unique))

        return body

    def mask_messages_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Deep scan and mask secrets in an Anthropic Messages-shape request body.

        The Messages API differs from chat completions: top-level `system` field, content
        blocks always lists (text / tool_use / tool_result), tool args nested as a dict
        instead of a JSON-encoded string. Kept as a separate walker so the OpenAI-shape
        path stays untouched.

        Returns a masked copy — does NOT mutate the original.
        """
        if not self._secrets:
            return body

        body = copy.deepcopy(body)
        all_redacted: list[str] = []

        # Top-level `system` — string or list of text blocks.
        system = body.get("system")
        if isinstance(system, str):
            masked, r = self.mask_string(system)
            body["system"] = masked
            all_redacted.extend(r)
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str):
                        masked, r = self.mask_string(text)
                        block["text"] = masked
                        all_redacted.extend(r)

        # Messages — content is string or list of blocks.
        for msg in body.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                masked, r = self.mask_string(content)
                msg["content"] = masked
                all_redacted.extend(r)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        all_redacted.extend(self._mask_messages_block(block))

        if all_redacted:
            unique = list(dict.fromkeys(all_redacted))
            logger.info("Masked secrets in messages request: %s", ", ".join(unique))

        return body

    def _mask_messages_block(self, block: dict[str, Any]) -> list[str]:
        """Mask secrets inside a single Anthropic content block. Mutates in place."""
        redacted: list[str] = []
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                masked, r = self.mask_string(text)
                block["text"] = masked
                redacted.extend(r)

        elif block_type == "tool_use":
            # `input` is an arbitrary JSON-serializable dict — recurse into all string leaves.
            tool_input = block.get("input")
            if tool_input is not None:
                masked_input, r = self._mask_json_value(tool_input)
                block["input"] = masked_input
                redacted.extend(r)

        elif block_type == "tool_result":
            # `content` may be a string or a list of {type:"text", text:...} blocks.
            content = block.get("content")
            if isinstance(content, str):
                masked, r = self.mask_string(content)
                block["content"] = masked
                redacted.extend(r)
            elif isinstance(content, list):
                for sub in content:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        text = sub.get("text")
                        if isinstance(text, str):
                            masked, r = self.mask_string(text)
                            sub["text"] = masked
                            redacted.extend(r)

        return redacted

    def _mask_json_value(self, value: Any) -> tuple[Any, list[str]]:
        """Recursively mask string leaves in an arbitrary JSON value. Returns new value + redactions."""
        redacted: list[str] = []
        if isinstance(value, str):
            masked, r = self.mask_string(value)
            return masked, r
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                new_v, r = self._mask_json_value(v)
                out[k] = new_v
                redacted.extend(r)
            return out, redacted
        if isinstance(value, list):
            out_list: list[Any] = []
            for item in value:
                new_v, r = self._mask_json_value(item)
                out_list.append(new_v)
                redacted.extend(r)
            return out_list, redacted
        return value, redacted

    def _mask_message(self, msg: dict[str, Any]) -> list[str]:
        """Mask secrets in a single message dict. Mutates in place. Returns redacted var names."""
        redacted: list[str] = []

        # Handle content field
        content = msg.get("content")
        if isinstance(content, str):
            masked, r = self.mask_string(content)
            msg["content"] = masked
            redacted.extend(r)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        masked, r = self.mask_string(text)
                        item["text"] = masked
                        redacted.extend(r)

        # Handle tool_calls
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                func = tc.get("function")
                if isinstance(func, dict):
                    args = func.get("arguments")
                    if isinstance(args, str):
                        masked, r = self.mask_string(args)
                        func["arguments"] = masked
                        redacted.extend(r)

        return redacted

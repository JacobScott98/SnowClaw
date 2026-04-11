"""Tests for SecretMasker.mask_messages_request — Anthropic Messages-shape walker."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add proxy/ to path so we can import the modules directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "proxy"))

from masking import SecretMasker  # noqa: E402


SECRET = "supersecretvalue123"


@pytest.fixture
def masker() -> SecretMasker:
    """SecretMasker configured to mask TEST_SECRET."""
    with patch.dict(os.environ, {"SNOWCLAW_MASK_VARS": "TEST_SECRET", "TEST_SECRET": SECRET}):
        return SecretMasker()


# ---------------------------------------------------------------------------
# Top-level system field
# ---------------------------------------------------------------------------


class TestSystemField:
    def test_system_string_with_secret_is_masked(self, masker: SecretMasker):
        body = {
            "model": "claude-sonnet-4-6",
            "system": f"You are an agent. Token={SECRET} please.",
            "messages": [],
        }
        out = masker.mask_messages_request(body)
        assert SECRET not in out["system"]
        assert "[REDACTED:TEST_SECRET]" in out["system"]

    def test_system_block_list_with_secret_is_masked(self, masker: SecretMasker):
        body = {
            "model": "claude-sonnet-4-6",
            "system": [
                {"type": "text", "text": f"prefix {SECRET} suffix"},
                {"type": "text", "text": "no secret here"},
            ],
            "messages": [],
        }
        out = masker.mask_messages_request(body)
        assert "[REDACTED:TEST_SECRET]" in out["system"][0]["text"]
        assert SECRET not in out["system"][0]["text"]
        assert out["system"][1]["text"] == "no secret here"

    def test_system_block_with_cache_control_preserved(self, masker: SecretMasker):
        """cache_control markers must survive masking — they're injected by OpenClaw."""
        body = {
            "model": "claude-sonnet-4-6",
            "system": [
                {"type": "text", "text": f"text {SECRET}", "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [],
        }
        out = masker.mask_messages_request(body)
        assert out["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert "[REDACTED:TEST_SECRET]" in out["system"][0]["text"]


# ---------------------------------------------------------------------------
# Message content
# ---------------------------------------------------------------------------


class TestMessageContent:
    def test_string_content_is_masked(self, masker: SecretMasker):
        body = {
            "messages": [{"role": "user", "content": f"my key is {SECRET}"}],
        }
        out = masker.mask_messages_request(body)
        assert SECRET not in out["messages"][0]["content"]
        assert "[REDACTED:TEST_SECRET]" in out["messages"][0]["content"]

    def test_text_block_content_is_masked(self, masker: SecretMasker):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"use {SECRET}"},
                        {"type": "text", "text": "no secret"},
                    ],
                }
            ],
        }
        out = masker.mask_messages_request(body)
        assert "[REDACTED:TEST_SECRET]" in out["messages"][0]["content"][0]["text"]
        assert out["messages"][0]["content"][1]["text"] == "no secret"


# ---------------------------------------------------------------------------
# tool_use blocks (nested input dict)
# ---------------------------------------------------------------------------


class TestToolUseBlocks:
    def test_tool_use_input_top_level_secret(self, masker: SecretMasker):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "auth",
                            "input": {"token": SECRET},
                        }
                    ],
                }
            ],
        }
        out = masker.mask_messages_request(body)
        assert out["messages"][0]["content"][0]["input"]["token"] == "[REDACTED:TEST_SECRET]"

    def test_tool_use_input_nested_dict_secret(self, masker: SecretMasker):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "auth",
                            "input": {
                                "config": {"credentials": {"api_key": f"prefix-{SECRET}-suffix"}},
                                "other": "untouched",
                            },
                        }
                    ],
                }
            ],
        }
        out = masker.mask_messages_request(body)
        nested = out["messages"][0]["content"][0]["input"]
        assert nested["config"]["credentials"]["api_key"] == "prefix-[REDACTED:TEST_SECRET]-suffix"
        assert nested["other"] == "untouched"

    def test_tool_use_input_list_with_secret(self, masker: SecretMasker):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "f",
                            "input": {"items": ["safe", SECRET, "also safe"]},
                        }
                    ],
                }
            ],
        }
        out = masker.mask_messages_request(body)
        items = out["messages"][0]["content"][0]["input"]["items"]
        assert items[0] == "safe"
        assert items[1] == "[REDACTED:TEST_SECRET]"
        assert items[2] == "also safe"

    def test_tool_use_input_non_string_values_untouched(self, masker: SecretMasker):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "f",
                            "input": {"count": 42, "ratio": 1.5, "active": True, "missing": None},
                        }
                    ],
                }
            ],
        }
        out = masker.mask_messages_request(body)
        inp = out["messages"][0]["content"][0]["input"]
        assert inp == {"count": 42, "ratio": 1.5, "active": True, "missing": None}


# ---------------------------------------------------------------------------
# tool_result blocks
# ---------------------------------------------------------------------------


class TestToolResultBlocks:
    def test_tool_result_string_content_masked(self, masker: SecretMasker):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "content": f"result: {SECRET}"}
                    ],
                }
            ],
        }
        out = masker.mask_messages_request(body)
        assert "[REDACTED:TEST_SECRET]" in out["messages"][0]["content"][0]["content"]

    def test_tool_result_block_list_content_masked(self, masker: SecretMasker):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "content": [
                                {"type": "text", "text": f"prefix {SECRET}"},
                                {"type": "text", "text": "clean"},
                            ],
                        }
                    ],
                }
            ],
        }
        out = masker.mask_messages_request(body)
        blocks = out["messages"][0]["content"][0]["content"]
        assert "[REDACTED:TEST_SECRET]" in blocks[0]["text"]
        assert blocks[1]["text"] == "clean"


# ---------------------------------------------------------------------------
# Mutation safety + no-op behavior
# ---------------------------------------------------------------------------


class TestMutationAndNoop:
    def test_does_not_mutate_input(self, masker: SecretMasker):
        body = {
            "system": f"hello {SECRET}",
            "messages": [{"role": "user", "content": f"hi {SECRET}"}],
        }
        out = masker.mask_messages_request(body)
        # Original retains the secret unchanged.
        assert SECRET in body["system"]
        assert SECRET in body["messages"][0]["content"]
        # Output has it masked.
        assert SECRET not in out["system"]
        assert SECRET not in out["messages"][0]["content"]

    def test_no_mask_vars_passes_through(self):
        """When SNOWCLAW_MASK_VARS is empty, the body is returned as-is (same object)."""
        with patch.dict(os.environ, {"SNOWCLAW_MASK_VARS": ""}, clear=False):
            m = SecretMasker()
        body = {"system": f"hello {SECRET}", "messages": []}
        out = m.mask_messages_request(body)
        # Same-object identity check — no work done at all
        assert out is body
        assert SECRET in out["system"]

    def test_short_secret_below_threshold_ignored(self):
        """Secrets ≤3 chars are skipped to avoid masking common substrings."""
        with patch.dict(os.environ, {"SNOWCLAW_MASK_VARS": "TINY", "TINY": "ab"}):
            m = SecretMasker()
        body = {"messages": [{"role": "user", "content": "abracadabra"}]}
        out = m.mask_messages_request(body)
        assert out["messages"][0]["content"] == "abracadabra"

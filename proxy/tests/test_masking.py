"""Tests for secret masking middleware."""

import copy
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_masker(mask_vars: str, env_overrides: dict[str, str] | None = None):
    """Create a SecretMasker with controlled environment."""
    from masking import SecretMasker

    env = dict(os.environ)
    env["SNOWCLAW_MASK_VARS"] = mask_vars
    if env_overrides:
        env.update(env_overrides)
    with patch.dict(os.environ, env, clear=True):
        return SecretMasker()


# --- Basic masking ---


def test_basic_mask_user_message():
    masker = _make_masker("GH_TOKEN", {"GH_TOKEN": "ghp_abc123secret"})
    body = {
        "messages": [
            {"role": "user", "content": "My token is ghp_abc123secret please help"}
        ]
    }
    result = masker.mask_request(body)
    assert result["messages"][0]["content"] == "My token is [REDACTED:GH_TOKEN] please help"


def test_multiple_secrets_in_one_message():
    masker = _make_masker(
        "TOKEN_A,TOKEN_B",
        {"TOKEN_A": "secret_aaa", "TOKEN_B": "secret_bbb"},
    )
    body = {
        "messages": [
            {"role": "user", "content": "A=secret_aaa and B=secret_bbb"}
        ]
    }
    result = masker.mask_request(body)
    assert "[REDACTED:TOKEN_A]" in result["messages"][0]["content"]
    assert "[REDACTED:TOKEN_B]" in result["messages"][0]["content"]
    assert "secret_aaa" not in result["messages"][0]["content"]
    assert "secret_bbb" not in result["messages"][0]["content"]


def test_secret_in_tool_result():
    masker = _make_masker("API_KEY", {"API_KEY": "sk-testkey123"})
    body = {
        "messages": [
            {"role": "tool", "tool_call_id": "1", "content": "Response with sk-testkey123 in it"}
        ]
    }
    result = masker.mask_request(body)
    assert result["messages"][0]["content"] == "Response with [REDACTED:API_KEY] in it"


def test_secret_in_assistant_message():
    masker = _make_masker("MY_SECRET", {"MY_SECRET": "supersecret42"})
    body = {
        "messages": [
            {"role": "assistant", "content": "I found supersecret42 in the config"}
        ]
    }
    result = masker.mask_request(body)
    assert result["messages"][0]["content"] == "I found [REDACTED:MY_SECRET] in the config"


def test_secret_in_system_message():
    masker = _make_masker("SYS_KEY", {"SYS_KEY": "system_secret_value"})
    body = {
        "messages": [
            {"role": "system", "content": "Use system_secret_value for auth"}
        ]
    }
    result = masker.mask_request(body)
    assert result["messages"][0]["content"] == "Use [REDACTED:SYS_KEY] for auth"


def test_secret_in_tool_call_arguments():
    masker = _make_masker("DB_PASS", {"DB_PASS": "p@ssw0rd!"})
    body = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {
                            "name": "query",
                            "arguments": '{"password": "p@ssw0rd!"}',
                        },
                    }
                ],
            }
        ]
    }
    result = masker.mask_request(body)
    args = result["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert "p@ssw0rd!" not in args
    assert "[REDACTED:DB_PASS]" in args


def test_secret_as_substring():
    masker = _make_masker("TOKEN", {"TOKEN": "abc123"})
    body = {
        "messages": [
            {"role": "user", "content": "prefix_abc123_suffix"}
        ]
    }
    result = masker.mask_request(body)
    assert result["messages"][0]["content"] == "prefix_[REDACTED:TOKEN]_suffix"


# --- Edge cases ---


def test_short_secrets_skipped():
    masker = _make_masker("SHORT", {"SHORT": "ab"})
    body = {
        "messages": [
            {"role": "user", "content": "The value ab should not be masked"}
        ]
    }
    result = masker.mask_request(body)
    assert result["messages"][0]["content"] == "The value ab should not be masked"


def test_empty_mask_vars_noop():
    masker = _make_masker("")
    body = {
        "messages": [
            {"role": "user", "content": "Nothing to mask here"}
        ]
    }
    result = masker.mask_request(body)
    assert result["messages"][0]["content"] == "Nothing to mask here"


def test_missing_env_var_skipped():
    masker = _make_masker("DOES_NOT_EXIST")
    body = {
        "messages": [
            {"role": "user", "content": "Should pass through"}
        ]
    }
    result = masker.mask_request(body)
    assert result["messages"][0]["content"] == "Should pass through"


def test_longer_secrets_matched_first():
    masker = _make_masker(
        "SHORT_TOKEN,LONG_TOKEN",
        {"SHORT_TOKEN": "abc123", "LONG_TOKEN": "abc123456"},
    )
    body = {
        "messages": [
            {"role": "user", "content": "Value: abc123456"}
        ]
    }
    result = masker.mask_request(body)
    # The longer secret should be replaced first
    assert "[REDACTED:LONG_TOKEN]" in result["messages"][0]["content"]
    assert "abc123456" not in result["messages"][0]["content"]


def test_content_as_array():
    masker = _make_masker("KEY", {"KEY": "my_secret_key"})
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Here is my_secret_key"},
                    {"type": "text", "text": "And again my_secret_key"},
                ],
            }
        ]
    }
    result = masker.mask_request(body)
    for item in result["messages"][0]["content"]:
        assert "my_secret_key" not in item["text"]
        assert "[REDACTED:KEY]" in item["text"]


def test_no_mutation_of_original():
    masker = _make_masker("TOKEN", {"TOKEN": "secret_value"})
    body = {
        "messages": [
            {"role": "user", "content": "Contains secret_value"}
        ]
    }
    original = copy.deepcopy(body)
    masker.mask_request(body)
    assert body == original


def test_redaction_logged_without_value(caplog):
    masker = _make_masker("MY_TOKEN", {"MY_TOKEN": "supersecret123"})
    body = {
        "messages": [
            {"role": "user", "content": "Here is supersecret123"}
        ]
    }
    with caplog.at_level(logging.INFO, logger="masking"):
        masker.mask_request(body)
    assert "MY_TOKEN" in caplog.text
    assert "supersecret123" not in caplog.text


# --- Integration with app.py ---


def test_masking_and_transforms_integration():
    """Masking runs before transforms in the full pipeline."""
    import httpx
    from fastapi.testclient import TestClient

    env = {
        "CORTEX_BASE_URL": "https://test.snowflakecomputing.com/api/v2/cortex/v1",
        "SNOWCLAW_MASK_VARS": "SECRET_VAR",
        "SECRET_VAR": "leaked_secret_42",
    }

    with patch.dict(os.environ, env):
        # Re-create the masker in app module
        from masking import SecretMasker

        new_masker = SecretMasker()

        with patch("app._masker", new_masker):
            from app import app

            client = TestClient(app)

            with patch("app._get_client") as mock_get_client:
                mock_client = AsyncMock()
                mock_resp = httpx.Response(
                    status_code=200,
                    json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
                    request=httpx.Request("POST", "https://fake"),
                )
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_get_client.return_value = mock_client

                client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "claude-3-5-sonnet",
                        "max_tokens": 100,
                        "messages": [
                            {"role": "user", "content": "Key is leaked_secret_42"}
                        ],
                    },
                )

                call_kwargs = mock_client.post.call_args
                sent_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

                # Secret should be masked
                assert "leaked_secret_42" not in sent_body["messages"][0]["content"]
                assert "[REDACTED:SECRET_VAR]" in sent_body["messages"][0]["content"]

                # Transforms should also have run
                assert "max_tokens" not in sent_body
                assert sent_body["max_completion_tokens"] == 100

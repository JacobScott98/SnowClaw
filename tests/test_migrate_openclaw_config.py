"""Tests for migrate_openclaw_config — auto-migration of pre-existing projects."""

from __future__ import annotations

import json
from pathlib import Path

from snowclaw.config import CORTEX_CLAUDE_CONTEXT_WINDOW, migrate_claude_context_window, migrate_openclaw_config


def _write_config(root: Path, config: dict) -> Path:
    path = root / "openclaw.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def _read_config(root: Path) -> dict:
    return json.loads((root / "openclaw.json").read_text())


# ---------------------------------------------------------------------------
# Happy path: old single `cortex` provider gets split
# ---------------------------------------------------------------------------


class TestProviderSplit:
    def test_old_cortex_provider_split_into_two(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex": {
                        "baseUrl": "http://localhost:8080/v1",
                        "apiKey": "${SNOWFLAKE_TOKEN}",
                        "api": "openai-completions",
                        "models": [
                            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
                            {"id": "openai-gpt-5.1", "name": "GPT-5.1"},
                        ],
                    }
                }
            },
            "agents": {"defaults": {"model": "cortex/claude-sonnet-4-6"}},
        })

        assert migrate_openclaw_config(tmp_path) is True

        cfg = _read_config(tmp_path)
        providers = cfg["models"]["providers"]
        assert "cortex" not in providers
        assert "cortex-openai" in providers
        assert "cortex-claude" in providers

        # cortex-claude carries the Claude model, anthropic-messages API.
        cc = providers["cortex-claude"]
        assert cc["api"] == "anthropic-messages"
        assert cc["baseUrl"] == "http://localhost:8080"
        assert cc["headers"]["anthropic-version"] == "2023-06-01"
        assert any(m["id"] == "claude-sonnet-4-6" for m in cc["models"])

        # cortex-openai carries everything else, openai-completions API.
        co = providers["cortex-openai"]
        assert co["api"] == "openai-completions"
        assert co["baseUrl"] == "http://localhost:8080/v1"
        assert any(m["id"] == "openai-gpt-5.1" for m in co["models"])
        assert not any(m["id"].startswith("claude") for m in co["models"])

    def test_default_model_prefix_rewritten(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex": {
                        "api": "openai-completions",
                        "models": [{"id": "claude-sonnet-4-6"}],
                    }
                }
            },
            "agents": {"defaults": {"model": "cortex/claude-sonnet-4-6"}},
        })
        migrate_openclaw_config(tmp_path)
        cfg = _read_config(tmp_path)
        assert cfg["agents"]["defaults"]["model"] == "cortex-claude/claude-sonnet-4-6"

    def test_default_model_for_openai_routes_to_cortex_openai(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {"providers": {"cortex": {"models": [{"id": "openai-gpt-5.1"}]}}},
            "agents": {"defaults": {"model": "cortex/openai-gpt-5.1"}},
        })
        migrate_openclaw_config(tmp_path)
        cfg = _read_config(tmp_path)
        assert cfg["agents"]["defaults"]["model"] == "cortex-openai/openai-gpt-5.1"

    def test_per_agent_model_overrides_rewritten(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {"providers": {"cortex": {"models": [{"id": "claude-sonnet-4-6"}]}}},
            "agents": {
                "defaults": {"model": "cortex/claude-sonnet-4-6"},
                "researcher": {"model": "cortex/claude-opus-4-6"},
                "scripter": {"model": "cortex/openai-gpt-5.1"},
            },
        })
        migrate_openclaw_config(tmp_path)
        cfg = _read_config(tmp_path)
        assert cfg["agents"]["researcher"]["model"] == "cortex-claude/claude-opus-4-6"
        assert cfg["agents"]["scripter"]["model"] == "cortex-openai/openai-gpt-5.1"

    def test_cache_retention_added(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {"providers": {"cortex": {"models": [{"id": "claude-sonnet-4-6"}]}}},
            "agents": {"defaults": {"model": "cortex/claude-sonnet-4-6"}},
        })
        migrate_openclaw_config(tmp_path)
        cfg = _read_config(tmp_path)
        assert cfg["agents"]["defaults"]["params"]["cacheRetention"] == "long"

    def test_existing_cache_retention_not_overwritten(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {"providers": {"cortex": {"models": [{"id": "claude-sonnet-4-6"}]}}},
            "agents": {"defaults": {
                "model": "cortex/claude-sonnet-4-6",
                "params": {"cacheRetention": "short", "temperature": 0.2},
            }},
        })
        migrate_openclaw_config(tmp_path)
        cfg = _read_config(tmp_path)
        params = cfg["agents"]["defaults"]["params"]
        assert params["cacheRetention"] == "short"  # preserved
        assert params["temperature"] == 0.2

    def test_missing_max_tokens_backfilled_with_default(self, tmp_path: Path):
        """Old configs that predate the maxTokens field should get the standard default."""
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex": {
                        "models": [
                            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
                            {"id": "openai-gpt-5.1", "name": "GPT-5.1"},
                        ],
                    }
                }
            },
            "agents": {"defaults": {"model": "cortex/claude-sonnet-4-6"}},
        })
        migrate_openclaw_config(tmp_path)
        cfg = _read_config(tmp_path)
        cc_models = cfg["models"]["providers"]["cortex-claude"]["models"]
        co_models = cfg["models"]["providers"]["cortex-openai"]["models"]
        assert cc_models[0]["maxTokens"] == 131072
        assert co_models[0]["maxTokens"] == 131072

    def test_custom_models_in_old_provider_preserved_verbatim(self, tmp_path: Path):
        """User-customized contextWindow / maxTokens should survive the split."""
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex": {
                        "models": [
                            {
                                "id": "claude-sonnet-4-6",
                                "name": "Claude Sonnet 4.6 (custom)",
                                "contextWindow": 99999,
                                "maxTokens": 12345,
                            },
                        ],
                    }
                }
            },
            "agents": {"defaults": {"model": "cortex/claude-sonnet-4-6"}},
        })
        migrate_openclaw_config(tmp_path)
        cfg = _read_config(tmp_path)
        cc_models = cfg["models"]["providers"]["cortex-claude"]["models"]
        assert cc_models[0]["contextWindow"] == 99999
        assert cc_models[0]["maxTokens"] == 12345
        assert cc_models[0]["name"] == "Claude Sonnet 4.6 (custom)"


# ---------------------------------------------------------------------------
# Idempotency / no-op cases
# ---------------------------------------------------------------------------


class TestNoop:
    def test_already_migrated_is_noop(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex-openai": {"api": "openai-completions", "models": []},
                    "cortex-claude": {"api": "anthropic-messages", "models": []},
                }
            },
            "agents": {"defaults": {"model": "cortex-claude/claude-sonnet-4-6"}},
        })
        before = _read_config(tmp_path)
        assert migrate_openclaw_config(tmp_path) is False
        after = _read_config(tmp_path)
        assert after == before

    def test_missing_openclaw_json_is_noop(self, tmp_path: Path):
        assert migrate_openclaw_config(tmp_path) is False

    def test_no_cortex_provider_is_noop(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {"providers": {"some-other-provider": {"models": []}}},
        })
        assert migrate_openclaw_config(tmp_path) is False

    def test_invalid_json_is_noop(self, tmp_path: Path):
        (tmp_path / "openclaw.json").write_text("{not json")
        assert migrate_openclaw_config(tmp_path) is False

    def test_unrelated_keys_preserved(self, tmp_path: Path):
        """Channels, tools, gateway, custom keys must survive untouched."""
        _write_config(tmp_path, {
            "gateway": {"auth": {"mode": "none"}},
            "models": {"providers": {"cortex": {"models": [{"id": "claude-sonnet-4-6"}]}}},
            "channels": {"slack": {"enabled": True}},
            "tools": {"web": {"search": {"provider": "brave"}}},
            "agents": {"defaults": {"model": "cortex/claude-sonnet-4-6"}},
            "customKey": {"nested": "value"},
        })
        migrate_openclaw_config(tmp_path)
        cfg = _read_config(tmp_path)
        assert cfg["gateway"] == {"auth": {"mode": "none"}}
        assert cfg["channels"] == {"slack": {"enabled": True}}
        assert cfg["tools"] == {"web": {"search": {"provider": "brave"}}}
        assert cfg["customKey"] == {"nested": "value"}


# ---------------------------------------------------------------------------
# Claude context window migration (200K → 1M)
# ---------------------------------------------------------------------------


class TestClaudeContextWindowMigration:
    def test_upgrades_200k_to_1m(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex-claude": {
                        "api": "anthropic-messages",
                        "models": [
                            {"id": "claude-sonnet-4-6", "contextWindow": 200000, "maxTokens": 131072},
                            {"id": "claude-opus-4-6", "contextWindow": 200000, "maxTokens": 131072},
                        ],
                    },
                    "cortex-openai": {
                        "api": "openai-completions",
                        "models": [{"id": "openai-gpt-5.1", "contextWindow": 1047576}],
                    },
                }
            },
        })
        assert migrate_claude_context_window(tmp_path) is True
        cfg = _read_config(tmp_path)
        cc_models = cfg["models"]["providers"]["cortex-claude"]["models"]
        assert cc_models[0]["contextWindow"] == CORTEX_CLAUDE_CONTEXT_WINDOW
        assert cc_models[1]["contextWindow"] == CORTEX_CLAUDE_CONTEXT_WINDOW
        # OpenAI models untouched
        co_models = cfg["models"]["providers"]["cortex-openai"]["models"]
        assert co_models[0]["contextWindow"] == 1047576

    def test_custom_context_window_preserved(self, tmp_path: Path):
        """User-customized contextWindow (not 200K) should not be overwritten."""
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex-claude": {
                        "api": "anthropic-messages",
                        "models": [
                            {"id": "claude-sonnet-4-6", "contextWindow": 99999},
                        ],
                    },
                }
            },
        })
        assert migrate_claude_context_window(tmp_path) is False
        cfg = _read_config(tmp_path)
        assert cfg["models"]["providers"]["cortex-claude"]["models"][0]["contextWindow"] == 99999

    def test_already_1m_is_noop(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex-claude": {
                        "api": "anthropic-messages",
                        "models": [
                            {"id": "claude-sonnet-4-6", "contextWindow": CORTEX_CLAUDE_CONTEXT_WINDOW},
                        ],
                    },
                }
            },
        })
        assert migrate_claude_context_window(tmp_path) is False

    def test_missing_file_is_noop(self, tmp_path: Path):
        assert migrate_claude_context_window(tmp_path) is False

    def test_handles_old_cortex_provider(self, tmp_path: Path):
        """Pre-split configs with old `cortex` provider should also be migrated."""
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex": {
                        "models": [
                            {"id": "claude-sonnet-4-6", "contextWindow": 200000},
                            {"id": "openai-gpt-5.1", "contextWindow": 1047576},
                        ],
                    },
                }
            },
        })
        assert migrate_claude_context_window(tmp_path) is True
        cfg = _read_config(tmp_path)
        models = cfg["models"]["providers"]["cortex"]["models"]
        assert models[0]["contextWindow"] == CORTEX_CLAUDE_CONTEXT_WINDOW
        assert models[1]["contextWindow"] == 1047576  # non-Claude untouched

    def test_unrelated_keys_preserved(self, tmp_path: Path):
        _write_config(tmp_path, {
            "models": {
                "providers": {
                    "cortex-claude": {
                        "api": "anthropic-messages",
                        "baseUrl": "http://localhost:8080",
                        "models": [
                            {"id": "claude-sonnet-4-6", "contextWindow": 200000, "maxTokens": 131072},
                        ],
                    },
                }
            },
            "agents": {"defaults": {"model": "cortex-claude/claude-sonnet-4-6"}},
            "channels": {"slack": {"enabled": True}},
        })
        migrate_claude_context_window(tmp_path)
        cfg = _read_config(tmp_path)
        assert cfg["agents"]["defaults"]["model"] == "cortex-claude/claude-sonnet-4-6"
        assert cfg["channels"] == {"slack": {"enabled": True}}
        cc = cfg["models"]["providers"]["cortex-claude"]
        assert cc["baseUrl"] == "http://localhost:8080"
        assert cc["models"][0]["maxTokens"] == 131072

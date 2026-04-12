"""Tests for plugin management (snowclaw/plugins.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snowclaw.plugins import (
    _derive_id,
    _is_path_spec,
    load_plugins,
    plugins_add,
    plugins_remove,
    save_plugins,
)


# ---------------------------------------------------------------------------
# _derive_id
# ---------------------------------------------------------------------------


class TestDeriveId:
    def test_scoped_npm_package(self):
        assert _derive_id("@openclaw/voice-call") == "voice-call"

    def test_scoped_third_party(self):
        assert _derive_id("@memtensor/memos-cloud-plugin") == "memos-cloud-plugin"

    def test_unscoped_npm_package(self):
        assert _derive_id("voice-call") == "voice-call"

    def test_relative_path(self):
        assert _derive_id("./my-plugin") == "my-plugin"

    def test_nested_relative_path(self):
        assert _derive_id("./plugins/my-plugin") == "my-plugin"

    def test_absolute_path(self):
        assert _derive_id("/home/user/my-plugin") == "my-plugin"


class TestIsPathSpec:
    def test_relative(self):
        assert _is_path_spec("./my-plugin") is True

    def test_absolute(self):
        assert _is_path_spec("/abs/path") is True

    def test_npm(self):
        assert _is_path_spec("@openclaw/voice-call") is False

    def test_bare(self):
        assert _is_path_spec("voice-call") is False


# ---------------------------------------------------------------------------
# load / save round-trip
# ---------------------------------------------------------------------------


class TestLoadSave:
    def test_load_missing_file(self, tmp_path: Path):
        (tmp_path / ".snowclaw").mkdir()
        assert load_plugins(tmp_path) == []

    def test_load_no_snowclaw_dir(self, tmp_path: Path):
        assert load_plugins(tmp_path) == []

    def test_roundtrip(self, tmp_path: Path):
        plugins = [
            {"id": "voice-call", "source": "npm", "package": "@openclaw/voice-call"},
            {"id": "my-plugin", "source": "path", "path": "my-plugin"},
        ]
        save_plugins(tmp_path, plugins)

        loaded = load_plugins(tmp_path)
        assert loaded == plugins

    def test_save_creates_snowclaw_dir(self, tmp_path: Path):
        save_plugins(tmp_path, [])
        assert (tmp_path / ".snowclaw" / "plugins.json").exists()


# ---------------------------------------------------------------------------
# plugins_add
# ---------------------------------------------------------------------------


class TestPluginsAdd:
    def test_add_npm_plugin(self, tmp_path: Path):
        (tmp_path / ".snowclaw").mkdir()
        plugins_add(tmp_path, "@openclaw/voice-call")

        plugins = load_plugins(tmp_path)
        assert len(plugins) == 1
        assert plugins[0] == {
            "id": "voice-call",
            "source": "npm",
            "package": "@openclaw/voice-call",
        }

    def test_add_path_plugin(self, tmp_path: Path):
        (tmp_path / ".snowclaw").mkdir()
        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "index.js").write_text("// plugin")

        plugins_add(tmp_path, "./my-plugin")

        plugins = load_plugins(tmp_path)
        assert len(plugins) == 1
        assert plugins[0]["id"] == "my-plugin"
        assert plugins[0]["source"] == "path"
        assert plugins[0]["path"] == "my-plugin"

    def test_add_path_plugin_missing_dir(self, tmp_path: Path, capsys):
        (tmp_path / ".snowclaw").mkdir()
        plugins_add(tmp_path, "./nonexistent")

        # Should not be saved
        assert load_plugins(tmp_path) == []

    def test_add_duplicate_rejected(self, tmp_path: Path):
        (tmp_path / ".snowclaw").mkdir()
        plugins_add(tmp_path, "@openclaw/voice-call")
        plugins_add(tmp_path, "@openclaw/voice-call")

        plugins = load_plugins(tmp_path)
        assert len(plugins) == 1

    def test_add_multiple_plugins(self, tmp_path: Path):
        (tmp_path / ".snowclaw").mkdir()
        plugins_add(tmp_path, "@openclaw/voice-call")
        plugins_add(tmp_path, "@openclaw/matrix")

        plugins = load_plugins(tmp_path)
        assert len(plugins) == 2
        assert plugins[0]["id"] == "voice-call"
        assert plugins[1]["id"] == "matrix"


# ---------------------------------------------------------------------------
# plugins_remove
# ---------------------------------------------------------------------------


class TestPluginsRemove:
    def test_remove_existing(self, tmp_path: Path):
        plugins = [
            {"id": "voice-call", "source": "npm", "package": "@openclaw/voice-call"},
            {"id": "matrix", "source": "npm", "package": "@openclaw/matrix"},
        ]
        save_plugins(tmp_path, plugins)

        plugins_remove(tmp_path, "voice-call")

        remaining = load_plugins(tmp_path)
        assert len(remaining) == 1
        assert remaining[0]["id"] == "matrix"

    def test_remove_nonexistent(self, tmp_path: Path):
        (tmp_path / ".snowclaw").mkdir()
        # Should not raise, just print warning
        plugins_remove(tmp_path, "nonexistent")

    def test_remove_last_plugin(self, tmp_path: Path):
        save_plugins(tmp_path, [{"id": "voice-call", "source": "npm", "package": "@openclaw/voice-call"}])

        plugins_remove(tmp_path, "voice-call")

        remaining = load_plugins(tmp_path)
        assert remaining == []

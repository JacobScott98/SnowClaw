"""Tests for OpenClaw version normalization and build-time enforcement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snowclaw.scaffold import assemble_build_context
from snowclaw.utils import normalize_openclaw_version


def _make_project(tmp_path: Path, openclaw_version: str | None) -> Path:
    """Create a minimal project directory with a marker that may carry a version."""
    snowclaw_dir = tmp_path / ".snowclaw"
    snowclaw_dir.mkdir()
    marker: dict = {"database": "snowclaw_db", "schema": "snowclaw_schema"}
    if openclaw_version is not None:
        marker["openclaw_version"] = openclaw_version
    (snowclaw_dir / "config.json").write_text(json.dumps(marker))
    (tmp_path / "connections.toml").write_text("[default]\naccount = 'test'")
    return tmp_path


class TestNormalizeOpenClawVersion:
    def test_strips_lowercase_v_prefix(self):
        assert normalize_openclaw_version("v2026.4.15") == "2026.4.15"

    def test_strips_uppercase_v_prefix(self):
        assert normalize_openclaw_version("V2026.4.15") == "2026.4.15"

    def test_accepts_latest(self):
        assert normalize_openclaw_version("latest") == "latest"

    def test_accepts_calver(self):
        assert normalize_openclaw_version("2026.4.15") == "2026.4.15"
        assert normalize_openclaw_version("2025.12.1") == "2025.12.1"

    def test_strips_whitespace(self):
        assert normalize_openclaw_version("  v2026.4.15  ") == "2026.4.15"
        assert normalize_openclaw_version("\tlatest\n") == "latest"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "foo",
            "2026.4",
            "2026.4.15.1",
            "vv2026.4.15",
            "2026.04.15a",
            "LATEST",
        ],
    )
    def test_rejects_garbage(self, bad: str):
        with pytest.raises(ValueError):
            normalize_openclaw_version(bad)


class TestAssembleBuildContextVersion:
    def test_default_latest_substitutes(self, tmp_path: Path):
        root = _make_project(tmp_path, openclaw_version=None)

        build_dir = assemble_build_context(root)

        dockerfile = (build_dir / "Dockerfile").read_text()
        assert "ARG OPENCLAW_VERSION=latest" in dockerfile

    def test_calver_substitutes(self, tmp_path: Path):
        root = _make_project(tmp_path, openclaw_version="2026.4.15")

        build_dir = assemble_build_context(root)

        dockerfile = (build_dir / "Dockerfile").read_text()
        assert "ARG OPENCLAW_VERSION=2026.4.15" in dockerfile
        assert "ARG OPENCLAW_VERSION=latest" not in dockerfile

    def test_v_prefix_normalized_at_build_time(self, tmp_path: Path):
        root = _make_project(tmp_path, openclaw_version="v2026.4.15")

        build_dir = assemble_build_context(root)

        dockerfile = (build_dir / "Dockerfile").read_text()
        assert "ARG OPENCLAW_VERSION=2026.4.15" in dockerfile
        assert "ARG OPENCLAW_VERSION=v2026.4.15" not in dockerfile

    def test_invalid_marker_exits(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        root = _make_project(tmp_path, openclaw_version="garbage")

        with pytest.raises(SystemExit) as excinfo:
            assemble_build_context(root)

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Invalid openclaw_version" in captured.out

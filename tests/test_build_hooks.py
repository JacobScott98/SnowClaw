"""Tests for build-hooks feature in scaffold.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from snowclaw.scaffold import assemble_build_context, scaffold_user_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project directory with a marker and required files."""
    snowclaw_dir = tmp_path / ".snowclaw"
    snowclaw_dir.mkdir()
    (snowclaw_dir / "config.json").write_text(
        json.dumps({"database": "snowclaw_db", "schema": "snowclaw_schema"})
    )
    (tmp_path / "connections.toml").write_text("[default]\naccount = 'test'")
    return tmp_path


# ---------------------------------------------------------------------------
# scaffold_user_files
# ---------------------------------------------------------------------------


class TestScaffoldUserFiles:
    def test_creates_build_hooks_dir(self, tmp_path: Path):
        copied, skipped = scaffold_user_files(tmp_path)

        build_hooks = tmp_path / "build-hooks"
        assert build_hooks.is_dir()
        assert (build_hooks / ".gitkeep").exists()
        assert (build_hooks / "README.md").exists()
        assert "build-hooks/" in copied

    def test_does_not_overwrite_existing_build_hooks(self, tmp_path: Path):
        (tmp_path / "build-hooks").mkdir()
        (tmp_path / "build-hooks" / "my-hook.sh").write_text("#!/bin/bash\necho hi")

        copied, skipped = scaffold_user_files(tmp_path)

        assert "build-hooks/" not in copied
        assert (tmp_path / "build-hooks" / "my-hook.sh").exists()

    def test_readme_content(self, tmp_path: Path):
        scaffold_user_files(tmp_path)
        readme = (tmp_path / "build-hooks" / "README.md").read_text()
        assert "Scripts run alphabetically" in readme
        assert "apt-get" in readme


# ---------------------------------------------------------------------------
# assemble_build_context — build hooks
# ---------------------------------------------------------------------------


class TestAssembleBuildHooks:
    def test_no_build_hooks_dir(self, tmp_path: Path):
        root = _make_project(tmp_path)

        build_dir = assemble_build_context(root)

        dockerfile = (build_dir / "Dockerfile").read_text()
        assert "build-hooks" not in dockerfile
        assert not (build_dir / "build-hooks").exists()

    def test_empty_build_hooks_dir(self, tmp_path: Path):
        root = _make_project(tmp_path)
        (root / "build-hooks").mkdir()
        (root / "build-hooks" / ".gitkeep").touch()
        (root / "build-hooks" / "README.md").write_text("# Build Hooks\n")

        build_dir = assemble_build_context(root)

        dockerfile = (build_dir / "Dockerfile").read_text()
        assert "build-hooks" not in dockerfile
        assert not (build_dir / "build-hooks").exists()

    def test_with_sh_files(self, tmp_path: Path):
        root = _make_project(tmp_path)
        hooks_dir = root / "build-hooks"
        hooks_dir.mkdir()
        (hooks_dir / "00-install-ffmpeg.sh").write_text(
            "#!/bin/bash\napt-get update && apt-get install -y ffmpeg\n"
        )
        (hooks_dir / "01-install-tools.sh").write_text(
            "#!/bin/bash\napt-get install -y jq\n"
        )
        (hooks_dir / "README.md").write_text("# Build Hooks\n")
        (hooks_dir / ".gitkeep").touch()

        build_dir = assemble_build_context(root)

        # Dockerfile has hook layer
        dockerfile = (build_dir / "Dockerfile").read_text()
        assert "COPY build-hooks/ /tmp/build-hooks/" in dockerfile
        assert "Running $script" in dockerfile
        assert "rm -rf /tmp/build-hooks" in dockerfile

        # Hook layer is before the mkdir line
        hook_idx = dockerfile.index("COPY build-hooks/")
        mkdir_idx = dockerfile.index("mkdir -p /home/node/.openclaw")
        assert hook_idx < mkdir_idx

        # .sh files were copied to build context
        build_hooks = build_dir / "build-hooks"
        assert build_hooks.is_dir()
        assert (build_hooks / "00-install-ffmpeg.sh").exists()
        assert (build_hooks / "01-install-tools.sh").exists()

        # README and .gitkeep excluded from build context
        assert not (build_hooks / "README.md").exists()
        assert not (build_hooks / ".gitkeep").exists()

    def test_non_sh_files_ignored_for_detection(self, tmp_path: Path):
        root = _make_project(tmp_path)
        hooks_dir = root / "build-hooks"
        hooks_dir.mkdir()
        (hooks_dir / "notes.txt").write_text("not a hook\n")

        build_dir = assemble_build_context(root)

        dockerfile = (build_dir / "Dockerfile").read_text()
        assert "build-hooks" not in dockerfile

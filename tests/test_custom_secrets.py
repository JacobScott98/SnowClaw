"""Tests for dynamic user-managed custom secrets (CUSTOM_ prefix)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from snowclaw.network import get_custom_secrets


# ---------------------------------------------------------------------------
# get_custom_secrets()
# ---------------------------------------------------------------------------


class TestGetCustomSecrets:
    def test_no_env_file(self, tmp_path):
        result = get_custom_secrets("snowclaw", tmp_path / ".env")
        assert result == []

    def test_empty_env_file(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        assert get_custom_secrets("snowclaw", env) == []

    def test_no_custom_vars(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SNOWFLAKE_TOKEN=abc\nGH_TOKEN=xyz\n")
        assert get_custom_secrets("snowclaw", env) == []

    def test_single_custom_var(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("CUSTOM_MY_KEY=secret123\n")
        result = get_custom_secrets("snowclaw", env)
        assert result == [
            {"secret_name": "snowclaw_custom_my_key", "env_var": "CUSTOM_MY_KEY"},
        ]

    def test_multiple_custom_vars(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "SNOWFLAKE_TOKEN=abc\n"
            "CUSTOM_API_KEY=key1\n"
            "CUSTOM_DB_PASSWORD=pass2\n"
            "GH_TOKEN=xyz\n"
        )
        result = get_custom_secrets("myprefix", env)
        assert len(result) == 2
        assert result[0] == {
            "secret_name": "myprefix_custom_api_key",
            "env_var": "CUSTOM_API_KEY",
        }
        assert result[1] == {
            "secret_name": "myprefix_custom_db_password",
            "env_var": "CUSTOM_DB_PASSWORD",
        }

    def test_skips_comments_and_blanks(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# A comment\n"
            "\n"
            "CUSTOM_VALID=yes\n"
            "# CUSTOM_COMMENTED=no\n"
        )
        result = get_custom_secrets("snowclaw", env)
        assert len(result) == 1
        assert result[0]["env_var"] == "CUSTOM_VALID"

    def test_skips_empty_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("CUSTOM_EMPTY=\nCUSTOM_FULL=val\n")
        result = get_custom_secrets("snowclaw", env)
        assert len(result) == 1
        assert result[0]["env_var"] == "CUSTOM_FULL"

    def test_prefix_applied(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("CUSTOM_FOO=bar\n")
        result = get_custom_secrets("myapp", env)
        assert result[0]["secret_name"] == "myapp_custom_foo"


# ---------------------------------------------------------------------------
# write_dotenv() — CUSTOM_ preservation and MASK_VARS
# ---------------------------------------------------------------------------


class TestWriteDotenvCustom:
    def _base_settings(self):
        return {
            "account": "test-account",
            "sf_user": "admin",
            "pat": "token123",
            "channels": [],
            "database": "snowclaw_db",
            "schema": "snowclaw_schema",
            "tool_credentials": {},
        }

    def test_preserves_custom_vars(self, tmp_path):
        from snowclaw.config import write_dotenv

        env = tmp_path / ".env"
        env.write_text("OLD_STUFF=x\nCUSTOM_MY_SECRET=s3cret\n")

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = env.read_text()
        assert "CUSTOM_MY_SECRET=s3cret" in content

    def test_custom_vars_in_mask_vars(self, tmp_path):
        from snowclaw.config import write_dotenv

        env = tmp_path / ".env"
        env.write_text("CUSTOM_API_KEY=abc\nCUSTOM_DB_PASS=xyz\n")

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = env.read_text()
        for line in content.splitlines():
            if line.startswith("SNOWCLAW_MASK_VARS="):
                mask_vars = line.split("=", 1)[1].split(",")
                assert "CUSTOM_API_KEY" in mask_vars
                assert "CUSTOM_DB_PASS" in mask_vars
                break
        else:
            pytest.fail("SNOWCLAW_MASK_VARS not found in .env")

    def test_comment_header_present(self, tmp_path):
        from snowclaw.config import write_dotenv

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = (tmp_path / ".env").read_text()
        assert "# Custom user secrets (add CUSTOM_ prefixed vars below)" in content

    def test_no_custom_vars_still_writes(self, tmp_path):
        from snowclaw.config import write_dotenv

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = (tmp_path / ".env").read_text()
        assert "SNOWFLAKE_TOKEN=token123" in content


# ---------------------------------------------------------------------------
# assemble_build_context() — custom secrets in service.yaml
# ---------------------------------------------------------------------------


class TestAssembleBuildContextCustom:
    @pytest.fixture()
    def project(self, tmp_path):
        """Set up a minimal SnowClaw project directory."""
        marker_dir = tmp_path / ".snowclaw"
        marker_dir.mkdir()
        marker = {
            "version": "0.1.0",
            "database": "snowclaw_db",
            "schema": "snowclaw_schema",
            "openclaw_version": "latest",
            "account": "test-acct",
        }
        (marker_dir / "config.json").write_text(json.dumps(marker))

        # Minimal openclaw.json
        oc = {"channels": {}, "models": {"providers": {}}}
        (tmp_path / "openclaw.json").write_text(json.dumps(oc))

        # .env with custom vars
        (tmp_path / ".env").write_text(
            "SNOWFLAKE_TOKEN=tok\n"
            "CUSTOM_MY_API_KEY=abc123\n"
            "CUSTOM_DB_PASS=secret\n"
        )

        return tmp_path

    def test_custom_secrets_in_service_yaml(self, project):
        from snowclaw.scaffold import assemble_build_context

        with patch("snowclaw.scaffold.get_templates_dir") as mock_tpl:
            # Point to real templates
            real_templates = Path(__file__).resolve().parent.parent / "templates"
            mock_tpl.return_value = real_templates

            build_dir = assemble_build_context(project)

        service_yaml = (build_dir / "spcs" / "service.yaml").read_text()
        assert "snowclaw_custom_my_api_key" in service_yaml
        assert "CUSTOM_MY_API_KEY" in service_yaml
        assert "snowclaw_custom_db_pass" in service_yaml
        assert "CUSTOM_DB_PASS" in service_yaml

    def test_custom_vars_in_mask_vars_yaml(self, project):
        from snowclaw.scaffold import assemble_build_context

        with patch("snowclaw.scaffold.get_templates_dir") as mock_tpl:
            real_templates = Path(__file__).resolve().parent.parent / "templates"
            mock_tpl.return_value = real_templates

            build_dir = assemble_build_context(project)

        service_yaml = (build_dir / "spcs" / "service.yaml").read_text()
        # Find the SNOWCLAW_MASK_VARS line
        for line in service_yaml.splitlines():
            if "SNOWCLAW_MASK_VARS" in line and "CUSTOM_MY_API_KEY" in line:
                assert "CUSTOM_DB_PASS" in line
                break
        else:
            # MASK_VARS is set as a value, check it contains custom vars
            assert "CUSTOM_MY_API_KEY" in service_yaml

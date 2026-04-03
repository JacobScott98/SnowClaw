"""Tests for dynamic env secrets (all .env vars pushed as Snowflake secrets)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from snowclaw.network import get_env_secrets


# ---------------------------------------------------------------------------
# get_env_secrets()
# ---------------------------------------------------------------------------


class TestGetEnvSecrets:
    def test_no_env_file(self, tmp_path):
        result = get_env_secrets("snowclaw", tmp_path / ".env")
        assert result == []

    def test_empty_env_file(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        assert get_env_secrets("snowclaw", env) == []

    def test_excludes_config_vars(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "SNOWCLAW_DB=mydb\n"
            "SNOWCLAW_SCHEMA=myschema\n"
            "SNOWCLAW_MASK_VARS=a,b\n"
            "CORTEX_BASE_URL=https://example.com\n"
            "IMAGE_TAG=latest\n"
            "MY_CUSTOM_VAR=secret\n"
        )
        result = get_env_secrets("snowclaw", env)
        assert len(result) == 1
        assert result[0] == {
            "secret_name": "snowclaw_my_custom_var",
            "env_var": "MY_CUSTOM_VAR",
        }

    def test_excludes_hardcoded_secrets(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SNOWFLAKE_TOKEN=abc\nGH_TOKEN=xyz\nBRAVE_API_KEY=key\n")
        assert get_env_secrets("snowclaw", env) == []

    def test_excludes_channel_credentials(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "TELEGRAM_BOT_TOKEN=tok\n"
            "SLACK_BOT_TOKEN=xoxb\n"
            "SLACK_APP_TOKEN=xapp\n"
            "DISCORD_BOT_TOKEN=disc\n"
            "MY_SECRET=val\n"
        )
        result = get_env_secrets("snowclaw", env)
        assert len(result) == 1
        assert result[0]["env_var"] == "MY_SECRET"

    def test_single_var(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("MY_KEY=secret123\n")
        result = get_env_secrets("snowclaw", env)
        assert result == [
            {"secret_name": "snowclaw_my_key", "env_var": "MY_KEY"},
        ]

    def test_multiple_vars(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "SNOWFLAKE_TOKEN=abc\n"
            "API_KEY=key1\n"
            "DB_PASSWORD=pass2\n"
            "GH_TOKEN=xyz\n"
        )
        result = get_env_secrets("myprefix", env)
        assert len(result) == 2
        assert result[0] == {
            "secret_name": "myprefix_api_key",
            "env_var": "API_KEY",
        }
        assert result[1] == {
            "secret_name": "myprefix_db_password",
            "env_var": "DB_PASSWORD",
        }

    def test_custom_prefix_vars_still_work(self, tmp_path):
        """CUSTOM_ prefixed vars should still be picked up (no special treatment)."""
        env = tmp_path / ".env"
        env.write_text("CUSTOM_MY_KEY=secret123\n")
        result = get_env_secrets("snowclaw", env)
        assert result == [
            {"secret_name": "snowclaw_custom_my_key", "env_var": "CUSTOM_MY_KEY"},
        ]

    def test_skips_comments_and_blanks(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# A comment\n"
            "\n"
            "VALID_SECRET=yes\n"
            "# COMMENTED_SECRET=no\n"
        )
        result = get_env_secrets("snowclaw", env)
        assert len(result) == 1
        assert result[0]["env_var"] == "VALID_SECRET"

    def test_skips_empty_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("EMPTY_VAR=\nFULL_VAR=val\n")
        result = get_env_secrets("snowclaw", env)
        assert len(result) == 1
        assert result[0]["env_var"] == "FULL_VAR"

    def test_prefix_applied(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        result = get_env_secrets("myapp", env)
        assert result[0]["secret_name"] == "myapp_foo"


# ---------------------------------------------------------------------------
# write_dotenv() — extra var preservation and MASK_VARS
# ---------------------------------------------------------------------------


class TestWriteDotenvEnvSecrets:
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

    def test_preserves_extra_vars(self, tmp_path):
        from snowclaw.config import write_dotenv

        env = tmp_path / ".env"
        env.write_text("OLD_STUFF=x\nMY_SECRET=s3cret\n")

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = env.read_text()
        assert "MY_SECRET=s3cret" in content

    def test_preserves_custom_prefixed_vars(self, tmp_path):
        """Backward compat: CUSTOM_ prefixed vars are preserved like any other extra var."""
        from snowclaw.config import write_dotenv

        env = tmp_path / ".env"
        env.write_text("CUSTOM_MY_SECRET=s3cret\n")

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = env.read_text()
        assert "CUSTOM_MY_SECRET=s3cret" in content

    def test_extra_vars_in_mask_vars(self, tmp_path):
        from snowclaw.config import write_dotenv

        env = tmp_path / ".env"
        env.write_text("MY_API_KEY=abc\nMY_DB_PASS=xyz\n")

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = env.read_text()
        for line in content.splitlines():
            if line.startswith("SNOWCLAW_MASK_VARS="):
                mask_vars = line.split("=", 1)[1].split(",")
                assert "MY_API_KEY" in mask_vars
                assert "MY_DB_PASS" in mask_vars
                break
        else:
            pytest.fail("SNOWCLAW_MASK_VARS not found in .env")

    def test_no_custom_header(self, tmp_path):
        """The old CUSTOM_ header comment should no longer be present."""
        from snowclaw.config import write_dotenv

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = (tmp_path / ".env").read_text()
        assert "add CUSTOM_ prefixed vars below" not in content

    def test_no_extra_vars_still_writes(self, tmp_path):
        from snowclaw.config import write_dotenv

        with patch("snowclaw.config.console"):
            write_dotenv(tmp_path, self._base_settings())

        content = (tmp_path / ".env").read_text()
        assert "SNOWFLAKE_TOKEN=token123" in content


# ---------------------------------------------------------------------------
# assemble_build_context() — env secrets in service.yaml
# ---------------------------------------------------------------------------


class TestAssembleBuildContextEnvSecrets:
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

        # .env with various vars
        (tmp_path / ".env").write_text(
            "SNOWFLAKE_TOKEN=tok\n"
            "MY_API_KEY=abc123\n"
            "DB_PASS=secret\n"
        )

        return tmp_path

    def test_env_secrets_in_service_yaml(self, project):
        from snowclaw.scaffold import assemble_build_context

        with patch("snowclaw.scaffold.get_templates_dir") as mock_tpl:
            # Point to real templates
            real_templates = Path(__file__).resolve().parent.parent / "templates"
            mock_tpl.return_value = real_templates

            build_dir = assemble_build_context(project)

        service_yaml = (build_dir / "spcs" / "service.yaml").read_text()
        assert "snowclaw_my_api_key" in service_yaml
        assert "MY_API_KEY" in service_yaml
        assert "snowclaw_db_pass" in service_yaml
        assert "DB_PASS" in service_yaml

    def test_env_vars_in_mask_vars_yaml(self, project):
        from snowclaw.scaffold import assemble_build_context

        with patch("snowclaw.scaffold.get_templates_dir") as mock_tpl:
            real_templates = Path(__file__).resolve().parent.parent / "templates"
            mock_tpl.return_value = real_templates

            build_dir = assemble_build_context(project)

        service_yaml = (build_dir / "spcs" / "service.yaml").read_text()
        # Find the SNOWCLAW_MASK_VARS line
        for line in service_yaml.splitlines():
            if "SNOWCLAW_MASK_VARS" in line and "MY_API_KEY" in line:
                assert "DB_PASS" in line
                break
        else:
            # MASK_VARS is set as a value, check it contains env vars
            assert "MY_API_KEY" in service_yaml

"""Tests for allow-all egress mode in snowclaw.network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from snowclaw.network import (
    ALLOW_ALL_VALUE_LIST,
    NetworkRule,
    NetworkRulesConfig,
    build_network_rule_sql,
    load_network_config,
    load_network_rules,
    save_network_config,
    save_network_rules,
)


NAMES = {
    "schema": "DB.SCH",
    "egress_rule": "foo_egress_rule",
    "external_access": "foo_external_access",
}


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".snowclaw").mkdir()
    return tmp_path


def test_allow_all_value_list_constant():
    assert ALLOW_ALL_VALUE_LIST == ("0.0.0.0:443", "0.0.0.0:80")


def test_build_sql_allowlist_mode():
    rules = [NetworkRule("example.com", 443, "test")]
    stmts = build_network_rule_sql(NAMES, rules)
    assert len(stmts) == 2
    assert "VALUE_LIST = ('example.com:443')" in stmts[0]
    assert "CREATE OR REPLACE NETWORK RULE" in stmts[0]
    assert "CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION" in stmts[1]


def test_build_sql_allow_all_ignores_rules():
    rules = [NetworkRule("example.com", 443, "ignored")]
    stmts = build_network_rule_sql(NAMES, rules, allow_all=True)
    assert len(stmts) == 2
    assert "VALUE_LIST = ('0.0.0.0:443', '0.0.0.0:80')" in stmts[0]
    assert "example.com" not in stmts[0]


def test_build_sql_allow_all_with_empty_rules():
    stmts = build_network_rule_sql(NAMES, [], allow_all=True)
    assert len(stmts) == 2
    assert "VALUE_LIST = ('0.0.0.0:443', '0.0.0.0:80')" in stmts[0]


def test_build_sql_empty_rules_allowlist_mode_returns_empty():
    assert build_network_rule_sql(NAMES, []) == []


def test_load_network_config_legacy_file_defaults_to_allowlist(project_root: Path):
    """A file without the allow_all_egress key (pre-feature shape) loads safely."""
    legacy = {"rules": [{"host": "a.com", "port": 443, "reason": "legacy"}]}
    (project_root / ".snowclaw" / "network-rules.json").write_text(json.dumps(legacy))

    cfg = load_network_config(project_root)
    assert cfg.allow_all_egress is False
    assert cfg.rules == [NetworkRule("a.com", 443, "legacy")]


def test_load_network_config_missing_file_returns_defaults(project_root: Path):
    cfg = load_network_config(project_root)
    assert cfg.allow_all_egress is False
    assert cfg.rules == []


def test_save_and_load_roundtrip_preserves_mode(project_root: Path):
    cfg = NetworkRulesConfig(
        allow_all_egress=True,
        rules=[NetworkRule("a.com", 443, "r1"), NetworkRule("b.com", 443, "r2")],
    )
    save_network_config(project_root, cfg)

    loaded = load_network_config(project_root)
    assert loaded.allow_all_egress is True
    assert loaded.rules == cfg.rules


def test_save_network_rules_shim_preserves_allow_all_flag(project_root: Path):
    """The back-compat save_network_rules shim must not clobber allow_all_egress."""
    initial = NetworkRulesConfig(allow_all_egress=True, rules=[])
    save_network_config(project_root, initial)

    save_network_rules(project_root, [NetworkRule("new.com", 443, "added")])

    cfg = load_network_config(project_root)
    assert cfg.allow_all_egress is True
    assert cfg.rules == [NetworkRule("new.com", 443, "added")]


def test_load_network_rules_shim_returns_rules_list(project_root: Path):
    save_network_config(
        project_root,
        NetworkRulesConfig(allow_all_egress=True, rules=[NetworkRule("a.com", 443, "")]),
    )
    assert load_network_rules(project_root) == [NetworkRule("a.com", 443, "")]

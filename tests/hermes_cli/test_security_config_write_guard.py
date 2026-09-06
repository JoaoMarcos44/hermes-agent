"""Security-policy configuration cannot be changed through agent-reachable writers."""

import os
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture
def isolated_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
        os.environ.pop("HERMES_MANAGED_DIR", None)
        from hermes_cli import config as cfg
        from hermes_cli import managed_scope

        cfg._LOAD_CONFIG_CACHE.clear()
        cfg._RAW_CONFIG_CACHE.clear()
        managed_scope.invalidate_managed_cache()
        yield home


def _config(home):
    path = home / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.mark.parametrize(
    "key",
    [
        "approvals.single_query_mode",
        "approvals.mode",
        "security.tirith_enabled",
        "command_allowlist",
    ],
)
def test_set_refuses_security_policy_keys(isolated_home, key, capsys):
    from hermes_cli.config import set_config_value

    with pytest.raises(SystemExit) as exc_info:
        set_config_value(key, "approve")

    assert exc_info.value.code == 1
    assert "security policy" in capsys.readouterr().err.lower()
    assert key.split(".", 1)[0] not in _config(isolated_home)


def test_unset_refuses_security_policy_keys(isolated_home, capsys):
    from hermes_cli.config import unset_config_value

    with pytest.raises(SystemExit) as exc_info:
        unset_config_value("approvals.single_query_mode")

    assert exc_info.value.code == 1
    assert "security policy" in capsys.readouterr().err.lower()
    assert _config(isolated_home) == {}


def test_config_set_command_rejects_force_for_single_query_policy(isolated_home, capsys):
    from types import SimpleNamespace

    from hermes_cli.config import _cmd_config_set

    with pytest.raises(SystemExit) as exc_info:
        _cmd_config_set(SimpleNamespace(
            key="approvals.single_query_mode", value="approve", force=True))

    assert exc_info.value.code == 1
    assert "operator-only" in capsys.readouterr().err.lower()
    assert _config(isolated_home) == {}


def test_operator_approval_command_keeps_its_supported_write_path(isolated_home):
    from hermes_cli.approval_mode import run_approval_mode_command

    result = run_approval_mode_command("off")

    assert result.ok is True
    assert result.mode == "off"
    assert _config(isolated_home)["approvals"]["mode"] == "off"


def test_non_security_config_writes_are_unchanged(isolated_home):
    from hermes_cli.config import set_config_value

    set_config_value("agent.max_turns", "17")

    assert _config(isolated_home)["agent"]["max_turns"] == 17


def test_non_security_write_preserves_existing_approval_policy(isolated_home):
    (isolated_home / "config.yaml").write_text(
        "approvals:\n  single_query_mode: approve\n", encoding="utf-8")
    from hermes_cli.config import set_config_value

    set_config_value("agent.max_turns", "17")

    config = _config(isolated_home)
    assert config["approvals"]["single_query_mode"] == "approve"
    assert config["agent"]["max_turns"] == 17

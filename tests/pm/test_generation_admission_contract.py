"""Admission contract for generated workspaces that must bootstrap Hermes PM."""

import pytest

from pm.package import InstallError
from pm.packages import _validate_pm_runtime_snapshot


def _pm_project(root, *, lock: bool) -> None:
    pm = root / "pm"
    pm.mkdir(parents=True)
    (pm / "pyproject.toml").write_text(
        '[project]\nname = "hermes-pm-fixture"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    if lock:
        (pm / "uv.lock").write_text("version = 1\n", encoding="utf-8")


def test_generated_workspace_missing_pm_lock_is_rejected(tmp_path):
    source = tmp_path / "source"
    workspace = tmp_path / "generation" / "workspace"
    _pm_project(source, lock=True)
    _pm_project(workspace, lock=False)

    missing = workspace / "pm" / "uv.lock"
    with pytest.raises(InstallError, match="prepared workspace is missing PM runtime input") as exc:
        _validate_pm_runtime_snapshot(source, workspace)

    assert str(missing) in str(exc.value)


def test_generated_workspace_with_complete_pm_inputs_is_accepted(tmp_path):
    source = tmp_path / "source"
    workspace = tmp_path / "generation" / "workspace"
    _pm_project(source, lock=True)
    _pm_project(workspace, lock=True)

    _validate_pm_runtime_snapshot(source, workspace)


def test_project_without_pm_runtime_has_no_snapshot_requirement(tmp_path):
    source = tmp_path / "source"
    workspace = tmp_path / "generation" / "workspace"
    source.mkdir()
    workspace.mkdir(parents=True)

    _validate_pm_runtime_snapshot(source, workspace)

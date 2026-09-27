"""Terminal-subshell PATH completion in ``tools/environments/local.py``.

A backend started by a non-interactive SSH session, systemd or a GUI launcher
inherits a PATH without ``~/.local/bin`` (only the login shell adds it), so CLIs
installed there were ``command not found`` from the terminal tool (#111778).
"""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools.environments import local as local_mod
from tools.environments.local import _append_missing_sane_path_entries, _make_run_env

pytestmark = pytest.mark.platforms("posix")  # POSIX PATH completion only


def test_existing_user_local_bin_appended_after_inherited_entries(monkeypatch, tmp_path):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", lambda: [])
    monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", lambda: [])
    monkeypatch.setattr(local_mod, "_resolve_hermes_bin_dir", lambda: None)

    entries = _make_run_env({})["PATH"].split(os.pathsep)

    assert entries[:2] == ["/usr/bin", "/bin"]
    assert entries.count(str(local_bin)) == 1
    # Already on PATH: position kept, no duplicate appended.
    already = _append_missing_sane_path_entries(f"{local_bin}:/usr/bin").split(":")
    assert already[0] == str(local_bin) and already.count(str(local_bin)) == 1


def test_missing_user_local_bin_not_appended(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", lambda: [])

    assert ".local" not in _append_missing_sane_path_entries("/usr/bin:/bin")


def _mock_python_selection(monkeypatch, tmp_path):
    tool_bin = tmp_path / "tools" / "python" / "bin"
    venv = tmp_path / "installs" / "selected" / "environments" / "current" / "venv"
    venv_bin = venv / "bin"
    tool_bin.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    for path, marker in ((tool_bin / "python3", "tool-python"), (venv_bin / "python3", "dependency-venv")):
        path.write_text(f"#!/bin/sh\\necho {marker}\\n")
        path.chmod(0o755)
    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: venv)
    monkeypatch.setattr(
        "pm.installed_package",
        lambda name: SimpleNamespace(binary=tool_bin / "python3") if name == "python" else None,
    )
    monkeypatch.setattr(local_mod, "_resolve_hermes_bin_dir", lambda: None)
    return tool_bin, venv_bin


def test_terminal_foreground_prefers_dependency_python_without_moving_other_tools(monkeypatch, tmp_path):
    tool_bin, venv_bin = _mock_python_selection(monkeypatch, tmp_path)
    node_bin = tmp_path / "tools" / "node" / "bin"
    node_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"/custom/bin:{node_bin}:{tool_bin}:/usr/bin")
    monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", lambda: [])
    monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", lambda: [str(node_bin), str(tool_bin)])

    entries = _make_run_env({})["PATH"].split(os.pathsep)

    assert entries[:4] == ["/custom/bin", str(node_bin), str(venv_bin), str(tool_bin)]
    assert entries.count(str(venv_bin)) == 1
    output = subprocess.check_output(["python3"], env={"PATH": os.pathsep.join(entries)}, text=True)
    assert output.strip() == "dependency-venv"


def test_terminal_background_pty_prefers_dependency_python(monkeypatch, tmp_path):
    tool_bin, venv_bin = _mock_python_selection(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", f"/custom/bin:{tool_bin}:/usr/bin")

    from tools.process_registry import ProcessRegistry

    entries = ProcessRegistry._spawn_env({})["PATH"].split(os.pathsep)

    assert entries[:3] == ["/custom/bin", str(venv_bin), str(tool_bin)]
    output = subprocess.check_output(["python3"], env={"PATH": os.pathsep.join(entries)}, text=True)
    assert output.strip() == "dependency-venv"


def test_terminal_python_path_is_unchanged_without_committed_environment(monkeypatch, tmp_path):
    tool_bin = tmp_path / "tools" / "python" / "bin"
    tool_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"/custom/bin:{tool_bin}:/usr/bin")
    monkeypatch.setattr(local_mod, "_git_bash_bin_dirs", lambda: [])
    monkeypatch.setattr(local_mod, "_managed_runtime_path_entries", lambda: [])
    monkeypatch.setattr(local_mod, "_resolve_hermes_bin_dir", lambda: None)
    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: None)

    assert _make_run_env({})["PATH"].split(os.pathsep)[:3] == [
        "/custom/bin", str(tool_bin), "/usr/bin",
    ]

"""Regression tests for issue #121932 at the shared environment boundary."""

import base64
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.code_execution_tool import _ship_file_to_remote
from tools.environments.base import BaseEnvironment
from tools.environments.managed_modal import ManagedModalEnvironment
from tools.environments.remote_file_delivery import (
    RPC_KERNEL_ENV_NAMES,
    source_and_remove_env_file,
)


SYNTHETIC_SUDO_PASSWORD = "synthetic_sudo_password_121932_not_a_real_credential"


class _UploadManager:
    def __init__(self):
        self.uploads = []

    def upload_file(self, host_path, remote_path):
        host = Path(host_path)
        self.uploads.append({
            "remote_path": remote_path,
            "content": host.read_bytes(),
            "directory_mode": stat.S_IMODE(host.parent.stat().st_mode),
            "file_mode": stat.S_IMODE(host.stat().st_mode),
        })


class _BaseHarness(BaseEnvironment):
    _stdin_mode = "heredoc"

    def __init__(self):
        super().__init__(cwd="/work", timeout=10)
        self.commands = []
        self._sync_manager = _UploadManager()
        self._snapshot_ready = True
        self._prepare_command = self._prepare
        self._before_execute = lambda: None
        self._wrap_command = lambda command, _cwd: command
        self._update_cwd = lambda _result: None
        self._wait_for_process = lambda proc, **_kwargs: proc.result
        self._run_bash = self._run

    @staticmethod
    def _prepare(command):
        if command == "sudo cat":
            return command, SYNTHETIC_SUDO_PASSWORD + "\n"
        return command, None

    def _run(self, command, *, login=False, timeout=120, stdin_data=None):
        del login, timeout, stdin_data
        self.commands.append(command)
        return SimpleNamespace(result={"output": "", "returncode": 0})

    def cleanup(self):
        pass


def _install_synchronous_deadline(monkeypatch):
    import agent.deadline

    monkeypatch.setattr(
        agent.deadline,
        "run_bounded_sync",
        lambda fn, *_args, **_kwargs: SimpleNamespace(timed_out=False, value=fn()),
    )
    monkeypatch.setattr(
        "tools.environments.base._new_output_collector",
        lambda *_args, **_kwargs: object(),
    )


def test_sdk_stdin_and_sudo_password_never_enter_command_text(monkeypatch):
    _install_synchronous_deadline(monkeypatch)
    env = _BaseHarness()

    result = env.execute("sudo cat", stdin_data="ordinary stdin payload")

    assert result["returncode"] == 0
    command_text = "\n".join(env.commands)
    assert SYNTHETIC_SUDO_PASSWORD not in command_text, "raw sudo password appeared in SDK command text"
    assert base64.b64encode(SYNTHETIC_SUDO_PASSWORD.encode()).decode() not in command_text
    staged = [upload["content"].decode() for upload in env._sync_manager.uploads]
    assert staged == [SYNTHETIC_SUDO_PASSWORD + "\nordinary stdin payload"]


@pytest.mark.platforms("linux", "macos")
def test_sdk_file_staging_uses_owner_only_local_directory_and_file():
    class UploadEnv:
        _stdin_mode = "heredoc"

        def __init__(self):
            self._sync_manager = _UploadManager()
            self.commands = []

        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            self.commands.append(command)
            return {"output": "", "returncode": 0}

    env = UploadEnv()
    _ship_file_to_remote(env, "/tmp/private/sandbox.env", "synthetic file payload")

    assert env._sync_manager.uploads, "SDK staging must use the file-sync upload channel"
    upload = env._sync_manager.uploads[0]
    assert upload["directory_mode"] == 0o700
    assert upload["file_mode"] == 0o600
    assert "chmod 600" in "\n".join(env.commands)


@pytest.mark.platforms("linux", "macos")
def test_private_env_scope_preserves_preexisting_exports(tmp_path):
    env_file = tmp_path / "kernel.env"
    env_file.write_text(
        "export PYTHONPATH=/private/kernel\n"
        "export PYTHONDONTWRITEBYTECODE=1\n",
        encoding="utf-8",
    )
    wrapped = source_and_remove_env_file(
        str(env_file),
        "printf 'child:%s|%s\n' \"$PYTHONPATH\" \"$PYTHONDONTWRITEBYTECODE\"",
        unset_names=RPC_KERNEL_ENV_NAMES,
    )
    script = (
        "export PYTHONPATH=/opt/session-lib\n"
        "export PYTHONDONTWRITEBYTECODE=session\n"
        f"{wrapped}\n"
        "printf 'parent:%s|%s\n' \"$PYTHONPATH\" \"$PYTHONDONTWRITEBYTECODE\"\n"
    )

    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "child:/private/kernel|1",
        "parent:/opt/session-lib|session",
    ]
    assert not env_file.exists()


def test_managed_modal_sudo_uses_stdin_payload_not_command_text():
    env = ManagedModalEnvironment.__new__(ManagedModalEnvironment)
    env.cwd = "/workspace"
    env.timeout = 20
    env._sandbox_id = "sandbox-test"
    env._persistent = False
    env._prepare_command = lambda command: (command, SYNTHETIC_SUDO_PASSWORD + "\n")
    captured = []
    body = {"status": "completed", "output": "ok", "returncode": 0}
    env._request = lambda *args, **kwargs: (
        captured.append(kwargs["json"]) or SimpleNamespace(status_code=200, json=lambda: body)
    )

    result = env.execute("sudo cat", stdin_data="ordinary stdin payload")

    assert result == {"output": "ok", "returncode": 0}
    payload = captured[0]
    assert SYNTHETIC_SUDO_PASSWORD not in payload["command"]
    assert base64.b64encode(SYNTHETIC_SUDO_PASSWORD.encode()).decode() not in payload["command"]
    assert payload["stdinData"] == SYNTHETIC_SUDO_PASSWORD + "\nordinary stdin payload"

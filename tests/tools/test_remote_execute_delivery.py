"""Regression tests for issue #121932: remote execute_code secret delivery and scratch isolation."""

import base64
import json
import re
import secrets
import shlex
import threading
import time
from pathlib import Path

import pytest

from tools.code_execution_tool import _ship_file_to_remote
from tools.code_kernel_remote import _spawn_remote_kernel
from tools.environments.remote_file_delivery import create_private_remote_dir, ensure_owner_scoped_results_dir
from tools.tool_result_storage import _resolve_storage_dir


SYNTHETIC_TOKEN = "synthetic_rpc_secret_121932_not_a_real_credential"


def _base64_decoded_argv(commands):
    text = "\n".join(commands)
    for candidate in re.findall(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{16,}={0,2}(?![A-Za-z0-9+/])", text):
        try:
            yield base64.b64decode(candidate, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue


def _assert_secret_absent_from_argv(commands, secret):
    argv_text = "\n".join(commands)
    assert secret not in argv_text, "raw synthetic secret appeared in remote command argv"
    assert secret not in "\n".join(_base64_decoded_argv(commands)), \
        "base64-decoded synthetic secret appeared in remote command argv"


class _UploadManager:
    def __init__(self, env):
        self.env = env

    def upload_file(self, host_path, remote_path):
        host = Path(host_path)
        data = host.read_bytes()
        self.env.remote_files[remote_path] = data
        self.env.uploaded_content[remote_path] = data.decode("utf-8")
        self.env.local_upload_modes.append((host.parent.stat().st_mode, host.stat().st_mode))


class _RemoteEnv:
    def __init__(self, stdin_mode="pipe", *, uid="1001", fail_after_source=False):
        self._stdin_mode = stdin_mode
        self._sync_manager = _UploadManager(self)
        self.uid = uid
        self.fail_after_source = fail_after_source
        self.commands = []
        self.remote_files = {}
        self.uploaded_content = {}
        self.remote_modes = {}
        self.remote_owners = {}
        self.local_upload_modes = []
        self.private_dirs = []
        self.mkdir_attempts = []
        self.sourced_env_files = []

    def get_temp_dir(self):
        return "/tmp"

    def execute(self, command, cwd=None, timeout=None, stdin_data=None, **_kwargs):
        self.commands.append(command)
        if command.startswith("ls -1 ") and "/req_*" in command:
            req_files = sorted(
                path for path in self.remote_files
                if "/req_" in path and not path.endswith(".tmp")
            )
            return {"output": "\n".join(req_files) + ("\n" if req_files else ""), "returncode": 0}
        if command.startswith("cat "):
            path = shlex.split(command)[1]
            payload = self.remote_files.get(path)
            return {
                "output": payload.decode("utf-8") if payload is not None else "",
                "returncode": 0 if payload is not None else 1,
            }
        if command.startswith("mv -f "):
            _, _, source, target = shlex.split(command)
            if source not in self.remote_files:
                return {"output": "", "returncode": 1}
            self.remote_files[target] = self.remote_files.pop(source)
            if source in self.remote_modes:
                self.remote_modes[target] = self.remote_modes.pop(source)
            return {"output": "", "returncode": 0}
        if command.startswith("rm -f "):
            for path in shlex.split(command)[2:]:
                self.remote_files.pop(path, None)
                self.remote_modes.pop(path, None)
            return {"output": "", "returncode": 0}
        if "id -u" in command:
            return {"output": f"{self.uid}\n", "returncode": 0}
        mkdir_command = command.split(" ||", 1)[0].rsplit("&& ", 1)[-1]
        if "mkdir -m 700 " in mkdir_command:
            args = shlex.split(mkdir_command)
            paths = args[3:4] if "2>/dev/null" in command else args[3:]
            self.mkdir_attempts.extend(paths)
            existing = [path for path in paths if path in self.private_dirs]
            if existing:
                if "2>/dev/null" not in command:
                    return {"output": "", "returncode": 1}
                if "stat -c %u" in command:
                    path = existing[0]
                    if self.remote_owners.get(path) != self.uid:
                        return {"output": "", "returncode": 1}
                    self.remote_modes[path] = 0o700
                    return {"output": "", "returncode": 0}
            for path in paths:
                if path not in self.private_dirs:
                    self.private_dirs.append(path)
                    self.remote_modes[path] = 0o700
                    self.remote_owners[path] = self.uid
        if stdin_data is not None and "cat >" in command:
            tokens = shlex.split(command)
            path = tokens[tokens.index(">") + 1]
            self.remote_files[path] = stdin_data.encode("utf-8")
            self.uploaded_content[path] = stdin_data
        if "chmod 600" in command:
            self.remote_modes[shlex.split(command)[-1]] = 0o600
        if command.startswith("rm -rf "):
            target = shlex.split(command)[-1]
            self.remote_files = {
                path: data for path, data in self.remote_files.items()
                if path != target and not path.startswith(target.rstrip("/") + "/")
            }
        if "kill -0" in command:
            return {"output": "ALIVE\n", "returncode": 0}

        env_file = next((path for path in self.remote_files if path.endswith(("/sandbox.env", "/kernel.env"))
                         and path in command), None)
        if env_file and (("python3 script.py" in command) or ("kernel_runner.py" in command)):
            self.sourced_env_files.append(env_file)
            if "rm -f" in command and env_file in command:
                self.remote_files.pop(env_file, None)
            if self.fail_after_source:
                raise RuntimeError("simulated transport failure after sourcing")
            if "kernel_runner.py" in command:
                return {"output": "PID:4242\n", "returncode": 0}
            return {"output": "script output\n", "returncode": 0}
        if self.fail_after_source and "python3 script.py" in command:
            raise RuntimeError("simulated transport failure before response")
        if "nohup" in command or "kernel_runner.py" in command:
            return {"output": "PID:4242\n", "returncode": 0}
        return {"output": "", "returncode": 0}


def _patch_token(monkeypatch, module):
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _size: SYNTHETIC_TOKEN)


def test_private_remote_directory_is_created_owner_only(monkeypatch):
    monkeypatch.setattr(secrets, "token_hex", lambda _size: "0123456789abcdef")
    env = _RemoteEnv()

    remote_dir = create_private_remote_dir(env, "hermes-private")

    assert remote_dir == "/tmp/hermes-private.0123456789abcdef"
    assert env.remote_modes[remote_dir] == 0o700
    assert env.mkdir_attempts == [remote_dir]


def test_private_remote_directory_fails_closed_when_name_exists(monkeypatch):
    monkeypatch.setattr(secrets, "token_hex", lambda _size: "0123456789abcdef")
    env = _RemoteEnv()
    remote_dir = "/tmp/hermes-private.0123456789abcdef"
    env.private_dirs.append(remote_dir)
    env.remote_modes[remote_dir] = 0o755

    with pytest.raises(RuntimeError):
        create_private_remote_dir(env, "hermes-private")

    assert env.mkdir_attempts == [remote_dir]
    assert env.remote_modes[remote_dir] == 0o755


@pytest.mark.parametrize("stdin_mode", ["pipe", "heredoc"])
def test_kernel_rpc_token_never_enters_raw_or_base64_decoded_argv(monkeypatch, stdin_mode):
    import tools.code_kernel_remote as kernel_remote

    _patch_token(monkeypatch, kernel_remote)
    env = _RemoteEnv(stdin_mode)
    kernel = _spawn_remote_kernel(
        env, "ssh", "owner", "task", frozenset({"read_file"}), idle_exit=1800)
    try:
        assert kernel is not None
        _assert_secret_absent_from_argv(env.commands, SYNTHETIC_TOKEN)
        env_file_content = env.uploaded_content.get(f"{kernel.kernel_dir}/kernel.env", "")
        assert SYNTHETIC_TOKEN in env_file_content
        env_names = {
            line.split("=", 1)[0].removeprefix("export ")
            for line in env_file_content.splitlines()
        }
        assert {
            "HERMES_KERNEL_DIR", "HERMES_RPC_DIR", "HERMES_RPC_TOKEN",
            "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
        } <= env_names
        env_file = f"{kernel.kernel_dir}/kernel.env"
        assert env.sourced_env_files == [env_file]
        assert not any(path.endswith("/kernel.env") for path in env.remote_files), \
            "kernel env file remained at rest after the runner was sourced"
        launch = next(command for command in env.commands if "nohup python3" in command)
        assert launch.index(f". {env_file}") < launch.index(f"rm -f {env_file}") \
            < launch.index("nohup python3"), "runner must inherit sourced values before the file is removed"
        assert env.remote_modes[env_file] == 0o600
        assert env.remote_modes[kernel.kernel_dir] == 0o700

        env.fail_after_source = True
        failed_kernel = _spawn_remote_kernel(
            env, "ssh", "owner", "task-failure", frozenset({"read_file"}), idle_exit=1800)
        assert failed_kernel is None
        assert not any(path.endswith("/kernel.env") for path in env.remote_files), \
            "failed launch left its staged kernel env file behind"
        assert any(command.startswith("rm -rf ") for command in env.commands)
    finally:
        if kernel is not None:
            kernel.kill()


@pytest.mark.parametrize("stdin_mode", ["pipe", "heredoc"])
def test_per_call_rpc_token_never_enters_raw_or_base64_decoded_argv(monkeypatch, stdin_mode):
    import tools.code_execution_tool as execution_tool

    _patch_token(monkeypatch, execution_tool)
    monkeypatch.setattr(execution_tool, "_rpc_poll_loop", lambda *_args, **_kwargs: None)
    env = _RemoteEnv(stdin_mode)
    result = json.loads(execution_tool._run_remote_per_call(
        env, "ssh", "print('ok')", "task", frozenset({"read_file"},),
        timeout=10, max_tool_calls=2, exec_start=0.0))

    assert result["status"] == "success"
    _assert_secret_absent_from_argv(env.commands, SYNTHETIC_TOKEN)
    env_files = [content for path, content in env.uploaded_content.items() if path.endswith("/sandbox.env")]
    assert len(env_files) == 1
    assert SYNTHETIC_TOKEN in env_files[0]
    env_names = {line.split("=", 1)[0].removeprefix("export ") for line in env_files[0].splitlines()}
    assert {"HERMES_RPC_DIR", "HERMES_RPC_TOKEN"} <= env_names
    assert not any(path.endswith("/sandbox.env") for path in env.remote_files)
    env_file_path = next(path for path in env.uploaded_content if path.endswith("/sandbox.env"))
    assert env.remote_modes[env_file_path] == 0o600
    assert len(env.sourced_env_files) == 1


@pytest.mark.parametrize("stdin_mode", ["pipe", "heredoc"])
def test_per_call_token_file_is_removed_on_transport_failure(monkeypatch, stdin_mode):
    import tools.code_execution_tool as execution_tool

    _patch_token(monkeypatch, execution_tool)
    monkeypatch.setattr(execution_tool, "_rpc_poll_loop", lambda *_args, **_kwargs: None)
    env = _RemoteEnv(stdin_mode, fail_after_source=True)
    result = json.loads(execution_tool._run_remote_per_call(
        env, "ssh", "print('ok')", "task", frozenset({"read_file"}),
        timeout=10, max_tool_calls=2, exec_start=0.0))

    assert result["status"] == "error"
    assert len(env.sourced_env_files) == 1, "transport failure must occur after env-file sourcing"
    assert not any(path.endswith("/sandbox.env") for path in env.remote_files)
    assert any(command.startswith("rm -rf ") for command in env.commands)


@pytest.mark.parametrize("stdin_mode", ["pipe", "heredoc"])
def test_rpc_response_payload_never_enters_command_argv(monkeypatch, stdin_mode):
    import tools.code_execution_rpc as rpc

    env = _RemoteEnv(stdin_mode)
    rpc_dir = "/tmp/hermes-rpc"
    request_path = f"{rpc_dir}/req_000001"
    response_path = f"{rpc_dir}/res_000001"
    response = "synthetic-private-tool-result-121932-never-a-real-secret"
    env.remote_files[request_path] = json.dumps({
        "seq": 1,
        "token": SYNTHETIC_TOKEN,
        "tool": "read_file",
        "args": {"path": "/private/example.txt"},
    }).encode("utf-8")
    monkeypatch.setattr(
        rpc,
        "_default_dispatch",
        lambda _task_id: lambda _tool, _args: response,
    )

    stop_event = threading.Event()
    worker = threading.Thread(
        target=rpc._rpc_poll_loop,
        args=(
            env,
            rpc_dir,
            "task",
            [],
            [0],
            2,
            frozenset({"read_file"}),
            stop_event,
            SYNTHETIC_TOKEN,
        ),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 2
    while response_path not in env.remote_files and time.monotonic() < deadline:
        time.sleep(0.01)
    stop_event.set()
    worker.join(timeout=1)

    assert env.remote_files[response_path].decode("utf-8") == response
    assert request_path not in env.remote_files
    _assert_secret_absent_from_argv(env.commands, response)


def test_sdk_file_delivery_detects_base64_argv_leaks():
    """The boundary test catches base64-only leaks even when raw-secret checks pass."""
    env = _RemoteEnv("heredoc")
    _ship_file_to_remote(env, "/tmp/private/sandbox.env", SYNTHETIC_TOKEN)
    _assert_secret_absent_from_argv(env.commands, SYNTHETIC_TOKEN)


def test_shared_temp_results_root_refuses_foreign_owned_precreated_directory():
    env = _RemoteEnv(uid="1001")
    results_dir = "/tmp/hermes-results-1001"
    env.private_dirs.append(results_dir)
    env.remote_modes[results_dir] = 0o777
    env.remote_owners[results_dir] = "2002"

    with pytest.raises(RuntimeError):
        ensure_owner_scoped_results_dir(env)

    assert env.remote_modes[results_dir] == 0o777, "foreign-owned directory must not be chmodded"
    ownership_check = next(command for command in env.commands if "stat -c %u" in command)
    assert ownership_check.index("stat -c %u") < ownership_check.index("chmod 700")


def test_shared_temp_results_root_allows_same_owner_reuse():
    env = _RemoteEnv(uid="1001")
    results_dir = "/tmp/hermes-results-1001"
    env.private_dirs.append(results_dir)
    env.remote_modes[results_dir] = 0o755
    env.remote_owners[results_dir] = env.uid

    assert ensure_owner_scoped_results_dir(env) == results_dir
    assert env.remote_modes[results_dir] == 0o700


def test_shared_temp_results_roots_are_scoped_to_remote_user():
    """Different remote OS UIDs get independently creatable private scratch roots."""
    first, second = _RemoteEnv(uid="1001"), _RemoteEnv(uid="1002")
    root_a = _resolve_storage_dir(first)
    root_b = _resolve_storage_dir(second)

    assert root_a != root_b
    assert root_a.endswith(f"hermes-results-{first.uid}")
    assert root_b.endswith(f"hermes-results-{second.uid}")
    assert root_a in first.private_dirs
    assert root_b in second.private_dirs
    assert first.remote_modes[root_a] == second.remote_modes[root_b] == 0o700
    assert _resolve_storage_dir(first) == root_a


@pytest.mark.platforms("windows")
def test_kernel_rpc_token_stays_out_of_base_session_snapshot_and_reaches_runner(
    monkeypatch, tmp_path,
):
    import shutil
    import time

    import tools.code_kernel_remote as kernel_remote
    from hermes_constants import get_hermes_home
    from tools.environments.local import LocalEnvironment, _windows_to_msys_path

    if not shutil.which("bash"):
        pytest.skip("bash required for the real BaseEnvironment snapshot path")

    scratch_root = get_hermes_home() / "cache" / "scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    remote_temp = _windows_to_msys_path(str(scratch_root))
    runner_token_path = (tmp_path / "runner-token.txt").as_posix()

    class SnapshotBackedLocalEnvironment(LocalEnvironment):
        def get_temp_dir(self):
            return remote_temp

        def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
            # Git Bash applies mode bits reliably only when directories are created under this umask.
            return super()._run_bash(
                f"umask 077\n{cmd_string}", login=login, timeout=timeout, stdin_data=stdin_data)

    env = SnapshotBackedLocalEnvironment(cwd=str(tmp_path), timeout=10)
    assert env._snapshot_ready, "BaseEnvironment session snapshot must be initialized"
    seeded = env.execute(
        "export PYTHONPATH=/opt/session-lib; "
        "export PYTHONDONTWRITEBYTECODE=session"
    )
    assert seeded["returncode"] == 0, seeded
    monkeypatch.setattr(kernel_remote.secrets, "token_urlsafe", lambda _size: SYNTHETIC_TOKEN)
    monkeypatch.setattr(
        kernel_remote,
        "REMOTE_KERNEL_RUNNER_SOURCE",
        "import os, time\n"
        f"open({runner_token_path!r}, 'w', encoding='utf-8').write(os.environ['HERMES_RPC_TOKEN'])\n"
        "while True: time.sleep(1)\n",
    )

    kernel = None
    try:
        kernel = _spawn_remote_kernel(
            env, "local-test", "owner", "task", frozenset({"read_file"}), idle_exit=1800)
        assert kernel is not None, "detached kernel runner should start"

        snapshot = env.execute(f"cat {shlex.quote(env._snapshot_path)}")
        assert snapshot["returncode"] == 0, snapshot
        assert SYNTHETIC_TOKEN not in snapshot["output"], \
            "synthetic RPC token class leaked into the persistent session snapshot"

        later_command = env.execute(
            "printf '%s|%s|%s' "
            "\"${HERMES_RPC_TOKEN-__absent__}\" "
            "\"${PYTHONPATH-__absent__}\" "
            "\"${PYTHONDONTWRITEBYTECODE-__absent__}\""
        )
        assert later_command["returncode"] == 0, later_command
        assert later_command["output"] == "__absent__|/opt/session-lib|session", \
            "kernel launch must not overwrite or clear the caller\'s pre-existing Python exports"

        token_received = Path(runner_token_path)
        for _ in range(100):
            if token_received.exists():
                break
            time.sleep(0.02)
        assert token_received.read_text(encoding="utf-8") == SYNTHETIC_TOKEN, \
            "detached runner did not receive the synthetic RPC token"
    finally:
        if kernel is not None:
            kernel.kill()
        (scratch_root / Path(env._snapshot_path).name).unlink(missing_ok=True)

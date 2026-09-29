"""A post-update gateway respawn must outlive its updater's Windows Job Object."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.platforms("windows")

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_for(predicate, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


def test_restart_watcher_child_survives_updater_job_teardown(tmp_path, _isolate_hermes_home):
    """Exercise the production watcher inside a real non-breakaway Job Object."""
    from hermes_cli.local_runtime.processes import _WindowsJob

    home = tmp_path / "hermes-home"
    home.mkdir()
    child_script = tmp_path / "respawned_gateway.py"
    child_pid_file = tmp_path / "gateway.pid"
    go_file = tmp_path / "go.marker"
    child_script.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()), encoding='utf-8')\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    driver = (
        "import pathlib, sys, time\n"
        f"go = pathlib.Path({str(go_file)!r})\n"
        "deadline = time.monotonic() + 30\n"
        "while not go.exists():\n"
        "    if time.monotonic() > deadline: raise SystemExit('assignment handshake timed out')\n"
        "    time.sleep(0.05)\n"
        "from hermes_cli._subprocess_compat import process_is_in_job\n"
        "if not process_is_in_job(): raise SystemExit('driver was not assigned to the real Job Object')\n"
        "from hermes_cli.gateway import _spawn_gateway_restart_watcher\n"
        f"ok = _spawn_gateway_restart_watcher(2147483647, [sys.executable, {str(child_script)!r}, {str(child_pid_file)!r}], host=False)\n"
        "if not ok: raise SystemExit('production restart watcher did not spawn')\n"
        f"pid_file = pathlib.Path({str(child_pid_file)!r})\n"
        "deadline = time.monotonic() + 30\n"
        "while not pid_file.exists():\n"
        "    if time.monotonic() > deadline: raise SystemExit('watcher did not start its child')\n"
        "    time.sleep(0.05)\n"
        "print(pid_file.read_text(encoding='utf-8'), flush=True)\n"
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    process = subprocess.Popen(
        [sys.executable, "-c", driver],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    job = _WindowsJob()
    gateway_pid = None
    try:
        job.assign(process)
        go_file.write_text("go", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=45)
        assert process.returncode == 0, f"watcher driver failed: {stderr}\n{stdout}"
        gateway_pid = int(stdout.strip())
        assert _wait_for(lambda: _alive(gateway_pid)), "respawned gateway never became live"

        # Closing the last real Job Object handle kills every contained process.
        job.close()
        job = None
        if not _wait_for(lambda: _alive(gateway_pid), timeout=3):
            pytest.fail(
                "respawned gateway died when the updater's non-breakaway Job Object closed",
                pytrace=False,
            )
    finally:
        go_file.unlink(missing_ok=True)
        if job is not None:
            job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if gateway_pid is not None and _alive(gateway_pid):
            try:
                process = psutil.Process(gateway_pid)
                process.kill()
                process.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass

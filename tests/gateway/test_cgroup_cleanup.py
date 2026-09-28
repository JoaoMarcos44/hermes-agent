"""Tests for the systemd ExecStopPost cgroup reaper (issue #37454)."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from gateway import cgroup_cleanup


class TestOwnCgroupPath:
    def test_parses_v2_cgroup_path(self, tmp_path, monkeypatch):
        proc_self = tmp_path / "cgroup"
        proc_self.write_text("0::/user.slice/user-1000.slice/hermes-gateway.service\n")
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: proc_self if p == "/proc/self/cgroup" else Path(p),
        )

        assert cgroup_cleanup._own_cgroup_path() == "/user.slice/user-1000.slice/hermes-gateway.service"


class TestReapCgroup:
    def test_noop_when_procs_file_missing(self, tmp_path, monkeypatch):
        cgroup_path = "/missing.slice/hermes-gateway.service"
        monkeypatch.setattr(
            cgroup_cleanup,
            "Path",
            lambda p: tmp_path / "does-not-exist" if "cgroup.procs" in p else Path(p),
        )

        def _explode(*_a, **_kw):
            pytest.fail("os.kill must not be called when cgroup.procs is unreadable")

        monkeypatch.setattr(cgroup_cleanup.os, "kill", _explode)
        assert cgroup_cleanup.reap_cgroup(cgroup_path) == 0

    def test_explicit_path_remains_available_outside_exec_stop_post(self, monkeypatch):
        monkeypatch.delenv("SERVICE_RESULT", raising=False)
        monkeypatch.setattr(cgroup_cleanup, "_read_cgroup_pids", lambda _path: [111, 222])
        monkeypatch.setattr(cgroup_cleanup.os, "getpid", lambda: 222)
        kill_signal = getattr(signal, "SIGKILL", 9)
        monkeypatch.setattr(cgroup_cleanup.signal, "SIGKILL", kill_signal, raising=False)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(cgroup_cleanup.os, "kill", lambda pid, sig: killed.append((pid, sig)))

        assert cgroup_cleanup.reap_cgroup("/stopped.service") == 1
        assert killed == [(111, kill_signal)]


class TestMain:
    def test_refuses_without_exec_stop_post_marker(self, monkeypatch, capsys):
        monkeypatch.delenv("SERVICE_RESULT", raising=False)
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.setattr(
            cgroup_cleanup,
            "reap_cgroup",
            lambda *_a, **_kw: pytest.fail("reaper must not run outside ExecStopPost"),
        )

        assert cgroup_cleanup.main() == 1
        assert "ExecStopPost-only" in capsys.readouterr().err

    def test_invocation_id_alone_does_not_authorize_reaping(self, monkeypatch):
        monkeypatch.delenv("SERVICE_RESULT", raising=False)
        monkeypatch.setenv("INVOCATION_ID", "live-gateway")
        monkeypatch.setattr(
            cgroup_cleanup,
            "reap_cgroup",
            lambda *_a, **_kw: pytest.fail("INVOCATION_ID is not an ExecStopPost capability"),
        )

        assert cgroup_cleanup.main() == 1

    @pytest.mark.parametrize("subcommand", ["run", "restart"])
    def test_stop_marker_refuses_while_gateway_is_still_live(
        self, monkeypatch, subcommand
    ):
        import gateway.status as status

        monkeypatch.setenv("SERVICE_RESULT", "success")
        monkeypatch.setattr(cgroup_cleanup, "_own_cgroup_path", lambda: "/gateway.service")
        monkeypatch.setattr(cgroup_cleanup, "_read_cgroup_pids", lambda _path: [111, 222])
        monkeypatch.setattr(cgroup_cleanup.os, "getpid", lambda: 222)
        monkeypatch.setattr(
            status,
            "_read_process_cmdline",
            lambda pid: (
                f"/opt/hermes/venv/bin/python -m hermes_cli.main gateway {subcommand}"
                if pid == 111
                else None
            ),
        )
        monkeypatch.setattr(
            cgroup_cleanup,
            "reap_cgroup",
            lambda *_a, **_kw: pytest.fail("live gateway must never be reaped"),
        )

        assert cgroup_cleanup.main() == 1

    def test_exec_stop_post_reaps_after_gateway_exits(self, monkeypatch):
        import gateway.status as status

        monkeypatch.setenv("SERVICE_RESULT", "success")
        monkeypatch.setattr(cgroup_cleanup, "_own_cgroup_path", lambda: "/gateway.service")
        monkeypatch.setattr(cgroup_cleanup, "_read_cgroup_pids", lambda _path: [111, 222])
        monkeypatch.setattr(cgroup_cleanup.os, "getpid", lambda: 222)
        monkeypatch.setattr(
            status,
            "_read_process_cmdline",
            lambda pid: "adb forward tcp:8888 tcp:8889" if pid == 111 else None,
        )
        calls: list[str] = []
        monkeypatch.setattr(
            cgroup_cleanup,
            "reap_cgroup",
            lambda path: calls.append(path) or 1,
        )

        assert cgroup_cleanup.main() == 0
        assert calls == ["/gateway.service"]

    def test_refuses_when_cgroup_cannot_be_resolved(self, monkeypatch):
        monkeypatch.setenv("SERVICE_RESULT", "success")
        monkeypatch.setattr(cgroup_cleanup, "_own_cgroup_path", lambda: None)
        monkeypatch.setattr(
            cgroup_cleanup,
            "reap_cgroup",
            lambda *_a, **_kw: pytest.fail("unknown cgroup must fail closed"),
        )

        assert cgroup_cleanup.main() == 1


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="requires Linux /proc")
@pytest.mark.parametrize("subcommand", ["run", "restart"])
def test_live_proc_gateway_runtime_is_detected(tmp_path, monkeypatch, subcommand):
    """Exercise the real /proc reader plus canonical gateway runtime matcher."""
    launcher = tmp_path / "hermes"
    launcher.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    launcher.chmod(0o755)
    proc = subprocess.Popen([str(launcher), "gateway", subcommand])
    try:
        monkeypatch.setattr(
            cgroup_cleanup,
            "_read_cgroup_pids",
            lambda _path: [proc.pid, os.getpid()],
        )
        assert cgroup_cleanup._cgroup_has_live_gateway("/test.service") is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)

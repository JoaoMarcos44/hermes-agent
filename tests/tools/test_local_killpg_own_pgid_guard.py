"""Process-group teardown contracts for the Darwin spawn-safety follow-on (#107029)."""

import signal
from types import SimpleNamespace

import psutil
import pytest

from tools.environments import local as local_mod
from tools.environments.local import _kill_process_group_posix


class _FakeChild:
    def __init__(self, pid: int):
        self.pid = pid
        self.kill_calls = 0

    def kill(self):
        self.kill_calls += 1


class _FakeProc:
    def __init__(self, pid: int):
        self.pid = pid
        self.kill_calls = 0
        self.wait_calls = []

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


def _install_descendants(monkeypatch, descendants):
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive=True: list(descendants)),
    )


@pytest.mark.parametrize("getpgrp_fails", [False, True])
def test_teardown_never_killpgs_own_or_unknown_process_group(
    monkeypatch, getpgrp_fails
):
    """A shared or unverifiable gateway group must use per-process cleanup."""
    gateway_pgid = 4242
    proc = _FakeProc(101)
    descendant = _FakeChild(202)
    killpg_calls = []

    monkeypatch.setattr(
        local_mod.os, "getpgid", lambda _pid: gateway_pgid, raising=False
    )
    if getpgrp_fails:

        def getpgrp():
            raise OSError("process group unavailable")

    else:
        getpgrp = lambda: gateway_pgid
    monkeypatch.setattr(local_mod.os, "getpgrp", getpgrp, raising=False)

    def killpg(pgid, sig):
        killpg_calls.append((pgid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(
        local_mod.os,
        "killpg",
        killpg,
        raising=False,
    )
    _install_descendants(monkeypatch, [descendant])

    _kill_process_group_posix(proc)

    assert killpg_calls == []
    assert descendant.kill_calls == 1
    assert proc.kill_calls == 1
    assert proc.wait_calls == [2.0]


@pytest.mark.parametrize("group_becomes_own", [False, True])
def test_teardown_rechecks_group_before_escalating(monkeypatch, group_becomes_own):
    """Keep group teardown for a distinct group, but recheck before SIGKILL."""
    child_pgid = 4343
    gateway_pgid = 4242
    proc = _FakeProc(101)
    descendant = _FakeChild(202)
    killpg_calls = []
    getpgrp_calls = 0

    monkeypatch.setattr(local_mod.os, "getpgid", lambda _pid: child_pgid, raising=False)

    def getpgrp():
        nonlocal getpgrp_calls
        getpgrp_calls += 1
        return child_pgid if group_becomes_own and getpgrp_calls > 1 else gateway_pgid

    monkeypatch.setattr(local_mod.os, "getpgrp", getpgrp, raising=False)
    monkeypatch.setattr(
        local_mod.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
        raising=False,
    )
    monkeypatch.setattr(
        local_mod,
        "_wait_for_group_exit",
        lambda *_args: not group_becomes_own,
    )
    monkeypatch.setattr(local_mod, "_sweep_escaped_descendants", lambda *_args: None)
    _install_descendants(monkeypatch, [descendant])

    _kill_process_group_posix(proc)

    assert killpg_calls == [(child_pgid, signal.SIGTERM)]
    assert proc.kill_calls == int(group_becomes_own)
    assert descendant.kill_calls == int(group_becomes_own)

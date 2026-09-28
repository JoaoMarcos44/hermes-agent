"""SIGKILL any process left in this systemd unit's cgroup.

Runs as ``ExecStopPost=`` after the gateway's main process has exited: the
safety net for long-lived helpers the gateway doesn't track (``adb``, platform
bridges) that would otherwise be orphaned in the cgroup and block
``Restart=always``.  Per-PID SIGKILLs over ``cgroup.procs`` are used instead of
writing ``1`` to ``cgroup.kill``: the kernel has returned ``EINVAL`` on the
cgroup-wide kill while per-PID signal delivery still works.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import sys
from pathlib import Path


def _own_cgroup_path() -> str | None:
    """Return the cgroup v2 path for the calling process, or None."""
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^0::(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _read_cgroup_pids(cgroup_path: str) -> list[int]:
    try:
        raw = Path(f"/sys/fs/cgroup{cgroup_path}/cgroup.procs").read_text(encoding="utf-8")
    except OSError:
        return []
    pids: list[int] = []
    for line in raw.splitlines():
        with contextlib.suppress(ValueError):
            pids.append(int(line.strip()))
    return pids


def _cgroup_has_live_gateway(cgroup_path: str) -> bool:
    """True when the cgroup still contains a live Hermes gateway runtime."""
    from gateway.status import _read_process_cmdline, looks_like_gateway_runtime_command_line

    for pid in _read_cgroup_pids(cgroup_path):
        if pid == os.getpid():
            continue
        if looks_like_gateway_runtime_command_line(_read_process_cmdline(pid)):
            return True
    return False


def reap_cgroup(cgroup_path: str | None = None) -> int:
    """SIGKILL every PID in the cgroup other than the caller. Returns the count killed."""
    cgroup_path = _own_cgroup_path() if cgroup_path is None else cgroup_path
    if not cgroup_path:
        return 0
    killed = 0
    for pid in _read_cgroup_pids(cgroup_path):
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGKILL)  # windows-footgun: ok — Linux-only (reads /proc, /sys/fs/cgroup; runs from a systemd unit)
            killed += 1
        except (ProcessLookupError, PermissionError):
            continue
    return killed


def main() -> int:
    # SERVICE_RESULT narrows the entry point to systemd's stop hooks, but it is
    # shared by ExecStop= and ExecStopPost=. The live-process check below is
    # what proves the destructive reap is actually in the post-stop state.
    if not os.environ.get("SERVICE_RESULT"):
        print(
            "cgroup_cleanup: refusing — this entry point is systemd ExecStopPost-only. "
            "Use reap_cgroup(cgroup_path) for an explicit stopped-service cgroup.",
            file=sys.stderr,
        )
        return 1

    cgroup_path = _own_cgroup_path()
    if not cgroup_path:
        print("cgroup_cleanup: refusing — could not resolve the caller's cgroup.", file=sys.stderr)
        return 1
    if _cgroup_has_live_gateway(cgroup_path):
        print(
            "cgroup_cleanup: refusing — a live gateway is still in this cgroup; "
            "cleanup is only safe after the gateway exits.",
            file=sys.stderr,
        )
        return 1

    reap_cgroup(cgroup_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

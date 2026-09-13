"""Regression tests for the owner-only hardening of ``state.db`` and its sidecars (#109728).

``_secure_state_db_files`` tightens ``state.db``/``-wal``/``-shm`` to ``0600``. The permission
hardening in #109509 did that with an ``os.open()`` / ``fchmod()`` / ``close()`` cycle on the live
files. POSIX record locks are owned per (process, inode), so ``close()`` on *any* descriptor for
the database cancels every lock this process holds on it — including the locks of an already-open
SQLite connection. The holder then looked dead to the next opener, which took the shared-memory DMS
exclusively at its own close, checkpointed, and unlinked ``-wal``/``-shm`` while the holder kept
writing to the deleted inodes: ``DeletedWalGenerationError`` and a session-store outage.

The tightening must therefore hold two properties at once, and both are pinned here:

1. it never cancels a POSIX lock this process holds on the database or its sidecars, and
2. the mode only ever lands on an inode the helper inspected — a symlink at the path is refused
   (the #109509 guarantee), and nothing that swaps the path afterwards can redirect the chmod.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_state_dbfile
from hermes_state import _secure_state_db_files
from hermes_state_dbfile import _LCHMOD, _O_PATH_CHMOD, tighten_db_file_mode


pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode bits and fcntl record locks are POSIX-only"
)

_SIDECAR_SUFFIXES = ("", "-wal", "-shm")


def _make_db_files(tmp_path: Path, mode: int = 0o644) -> Path:
    """A ``state.db`` plus ``-wal``/``-shm`` at *mode* (what a 0022 umask would leave)."""
    db_path = tmp_path / "state.db"
    for suffix in _SIDECAR_SUFFIXES:
        path = db_path.with_name(db_path.name + suffix)
        path.write_bytes(b"")
        os.chmod(path, mode)
    return db_path


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _child_can_lock(path: Path) -> bool:
    """Whether another process can take an exclusive record lock on *path* right now."""
    probe = (
        "import fcntl, sys\n"
        "handle = open(sys.argv[1], 'r+b')\n"
        "try:\n"
        "    fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
        "else:\n"
        "    print('ACQUIRED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return result.stdout.strip() == "ACQUIRED"


def test_existing_db_and_sidecars_are_tightened_to_owner_only(tmp_path):
    """The hardening still does its #109509 job: every file ends up at 0600."""
    db_path = _make_db_files(tmp_path)

    _secure_state_db_files(db_path)

    for suffix in _SIDECAR_SUFFIXES:
        path = db_path.with_name(db_path.name + suffix)
        assert _mode(path) == 0o600, f"{path.name} left at {oct(_mode(path))}"


def test_missing_main_db_is_created_owner_only(tmp_path):
    """A fresh profile store is private from its first byte, whatever the umask is."""
    db_path = tmp_path / "state.db"
    old_umask = os.umask(0o022)  # would leave 0644 if the mode were left to the umask
    try:
        _secure_state_db_files(db_path, create_main=True)
    finally:
        os.umask(old_umask)

    assert db_path.exists()
    assert _mode(db_path) == 0o600


def test_hardening_keeps_this_process_record_locks(tmp_path):
    """The root cause of #109728: tightening must not cancel locks this process holds.

    A descriptor closed by the helper would release the locks of every other descriptor this
    process has on the same inode, which is how a live gateway connection silently lost its
    WAL locks and a sibling opener went on to unlink the sidecars underneath it.
    """
    db_path = _make_db_files(tmp_path)
    handles = []
    try:
        for suffix in _SIDECAR_SUFFIXES:
            path = db_path.with_name(db_path.name + suffix)
            handle = path.open("r+b")
            handles.append(handle)
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert not _child_can_lock(path), "test setup: the lock was not taken"

        _secure_state_db_files(db_path, create_main=True)

        for suffix in _SIDECAR_SUFFIXES:
            path = db_path.with_name(db_path.name + suffix)
            assert not _child_can_lock(path), (
                f"tightening {path.name} cancelled this process's record lock — a live SQLite "
                "connection would have lost its locks the same way (#109728)"
            )
    finally:
        for handle in handles:
            handle.close()


def test_planted_symlink_at_a_sidecar_is_refused(tmp_path):
    """A symlink at the sidecar path never redirects the 0600 chmod (the #109509 guarantee)."""
    db_path = _make_db_files(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("not a sidecar", encoding="utf-8")
    os.chmod(victim, 0o644)
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_path.unlink()
    wal_path.symlink_to(victim)

    _secure_state_db_files(db_path)

    assert _mode(victim) == 0o644, "the planted symlink was followed"
    assert wal_path.is_symlink(), "the planted symlink was replaced instead of skipped"
    assert _mode(db_path) == 0o600, "the remaining files must still be tightened"


@pytest.mark.skipif(
    not (_O_PATH_CHMOD or _LCHMOD),
    reason="no O_PATH and no lchmod: the chmod target can only be named by path here",
)
def test_sidecar_swapped_for_a_symlink_before_the_chmod_is_not_followed(tmp_path, monkeypatch):
    """Nothing that replaces the path after the check can redirect the chmod.

    The swap is performed from inside ``os.chmod`` itself, which is exactly the window an
    ``lstat()``-then-``chmod()`` pair leaves open: the mode then lands on whatever the name
    resolves to the second time. Pinning the inode first (``O_PATH`` + ``/proc/self/fd``, or
    ``lchmod``) closes the window — the chmod still reaches the inode that was inspected.
    """
    db_path = _make_db_files(tmp_path)
    wal_path = db_path.with_name(db_path.name + "-wal")
    victim = tmp_path / "victim"
    victim.write_text("not a sidecar", encoding="utf-8")
    os.chmod(victim, 0o644)

    wal_stat = wal_path.stat()
    wal_identity = (wal_stat.st_dev, wal_stat.st_ino)
    real_chmod, real_stat = os.chmod, os.stat
    swapped: list[str] = []

    def racing_chmod(target, mode, **kwargs):
        # Fires whatever the chmod is aimed at — the path itself or a /proc/self/fd magic link —
        # as long as it currently resolves to the sidecar inode.
        if not swapped:
            try:
                target_stat = real_stat(target)
            except OSError:
                target_stat = None
            if target_stat is not None and (target_stat.st_dev, target_stat.st_ino) == wal_identity:
                swapped.append(str(target))
                wal_path.unlink()
                wal_path.symlink_to(victim)
        return real_chmod(target, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", racing_chmod)
    _secure_state_db_files(db_path)

    assert swapped, "the race was never simulated — the sidecar was not chmod'ed at all"
    assert _mode(victim) == 0o644, (
        "the chmod followed a symlink swapped in after the file was checked"
    )


def test_non_regular_sidecar_is_left_alone(tmp_path):
    """A directory (or any non-regular file) at a sidecar path is skipped, not an error."""
    db_path = _make_db_files(tmp_path)
    shm_path = db_path.with_name(db_path.name + "-shm")
    shm_path.unlink()
    shm_path.mkdir(mode=0o755)

    _secure_state_db_files(db_path)

    assert shm_path.is_dir()
    assert _mode(shm_path) == 0o755
    assert _mode(db_path) == 0o600


def test_tighten_db_file_mode_ignores_a_missing_path(tmp_path):
    """Sidecars only exist while a connection is open; a missing one is not an error."""
    tighten_db_file_mode(tmp_path / "state.db-wal")


@pytest.mark.skipif(not _O_PATH_CHMOD, reason="Linux O_PATH strategy only")
def test_linux_chmods_through_a_pinned_descriptor(tmp_path, monkeypatch):
    """On Linux the mode is applied to a pinned inode, never to a re-resolved path."""
    db_path = _make_db_files(tmp_path)
    targets: list[str] = []
    real_chmod = os.chmod

    def recording_chmod(target, mode, **kwargs):
        targets.append(str(target))
        return real_chmod(target, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    _secure_state_db_files(db_path)

    assert targets, "nothing was tightened"
    assert all(target.startswith("/proc/self/fd/") for target in targets), targets


def test_path_fallback_is_lock_safe_and_refuses_symlinks(tmp_path, monkeypatch):
    """Platforms without O_PATH take the lstat-then-chmod path; it must still never open the file.

    Exercised on every platform so the Linux CI lane covers the branch macOS and the BSDs run.
    """
    monkeypatch.setattr(hermes_state_dbfile, "_O_PATH_CHMOD", False)
    db_path = _make_db_files(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("not a sidecar", encoding="utf-8")
    os.chmod(victim, 0o644)
    shm_path = db_path.with_name(db_path.name + "-shm")
    shm_path.unlink()
    shm_path.symlink_to(victim)

    handle = db_path.open("r+b")
    try:
        fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

        _secure_state_db_files(db_path)

        assert not _child_can_lock(db_path), "the fallback cancelled this process's record lock"
        assert _mode(db_path) == 0o600
        assert _mode(db_path.with_name(db_path.name + "-wal")) == 0o600
        assert _mode(victim) == 0o644, "the fallback followed a planted symlink"
    finally:
        handle.close()


@pytest.mark.skipif(not _O_PATH_CHMOD, reason="Linux O_PATH strategy only")
def test_falls_back_to_the_path_when_proc_is_unavailable(tmp_path, monkeypatch):
    """A container without /proc mounted still gets owner-only modes."""
    db_path = _make_db_files(tmp_path)
    real_chmod = os.chmod

    def no_proc_chmod(target, mode, **kwargs):
        if str(target).startswith("/proc/"):
            raise FileNotFoundError(2, "No such file or directory", str(target))
        return real_chmod(target, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", no_proc_chmod)
    _secure_state_db_files(db_path)

    for suffix in _SIDECAR_SUFFIXES:
        path = db_path.with_name(db_path.name + suffix)
        assert _mode(path) == 0o600, f"{path.name} left at {oct(_mode(path))}"

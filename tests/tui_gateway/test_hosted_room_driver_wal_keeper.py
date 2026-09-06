"""WAL-keeper regression for the hosted-room poll lifecycle.

The gateway's idle hosted-room poll opens and closes the shared state.db every
cycle. A persistent content-touched connection must keep the WAL/SHM generation
alive while those ephemeral reads run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tui_gateway.hosted_room_driver import HostedRoomRuntime

from tests.tui_gateway.test_hosted_room_driver_runtime import (
    BINDING,
    FakeSessionRPC,
    RecordingTurnLocks,
    _wait_for,
)


def _ephemeral_cycle(db: Path) -> None:
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    finally:
        conn.close()


def _wal_path(db: Path) -> Path:
    return db.with_name(db.name + "-wal")


def _set_wal_mode(db: Path) -> None:
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL").fetchone()
    finally:
        conn.close()


@pytest.mark.linux_only
def test_driver_keeps_wal_sidecars_across_ephemeral_cycles(tmp_path: Path):
    db = tmp_path / "state.db"
    _set_wal_mode(db)
    runtime = HostedRoomRuntime(
        db_path=db,
        rooms=[BINDING],
        rpc=FakeSessionRPC(),
        turn_lock=RecordingTurnLocks(),
        poll_interval_seconds=0.01,
    )
    try:
        runtime.start()
        _wait_for(lambda: runtime._wal_keeper is not None)

        writer = sqlite3.connect(db, timeout=10)
        try:
            writer.execute("CREATE TABLE IF NOT EXISTS probe (k TEXT)")
            writer.execute("INSERT INTO probe VALUES ('v')")
            writer.commit()
            assert _wal_path(db).exists(), "writer's WAL sidecar missing"
        finally:
            writer.close()

        for _ in range(3):
            _ephemeral_cycle(db)
        assert _wal_path(db).exists(), (
            "ephemeral close deleted the WAL sidecar while the keeper was open"
        )
    finally:
        runtime.stop(timeout=2.0)
    assert runtime._wal_keeper is None, "keeper must close on stop"


def test_keeper_acquire_retries_after_transient_failure(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _set_wal_mode(db)
    runtime = HostedRoomRuntime(
        db_path=db,
        rooms=[BINDING],
        rpc=FakeSessionRPC(),
        turn_lock=RecordingTurnLocks(),
        poll_interval_seconds=0.01,
    )
    original_connect = sqlite3.connect
    calls = {"n": 0}

    def flaky_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", flaky_connect)
    runtime.start()
    _wait_for(lambda: runtime._wal_keeper is not None, timeout=2.0)
    assert calls["n"] >= 2, "acquire must be retried after a transient failure"
    runtime.stop(timeout=2.0)
    assert runtime._wal_keeper is None


def test_failed_acquire_does_not_leak_connection(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _set_wal_mode(db)
    runtime = HostedRoomRuntime(
        db_path=db,
        rooms=[BINDING],
        rpc=FakeSessionRPC(),
        turn_lock=RecordingTurnLocks(),
        poll_interval_seconds=0.01,
    )
    closed = []

    class _BrokenConn:
        def close(self):
            closed.append(True)

        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: _BrokenConn())
    runtime._acquire_wal_keeper()
    assert runtime._wal_keeper is None, "keeper must stay unset on failure"
    assert closed == [True], f"broken connection must close exactly once: {closed}"
    assert runtime._last_error and "wal keeper unavailable" in runtime._last_error

"""Regression coverage for live-but-stale external cron execution recovery (#115692)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import cron.executions as executions


@pytest.fixture()
def ledger(monkeypatch, tmp_path: Path):
    path = tmp_path / "cron" / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", path)
    return path


def _running_external_execution(ledger: Path, *, progress_at: float) -> str:
    record = executions.create_execution("stuck-job", source="builtin")
    assert executions.mark_execution_running(record["id"]) is not None
    with executions._transaction() as conn:
        conn.execute(
            """UPDATE executions
               SET process_id=?, pid=?, process_started_at=?, progress_at=?
               WHERE id=?""",
            ("deadlocked-worker", 4242, 123456, progress_at, record["id"]),
        )
    return record["id"]


def test_stale_live_external_owner_is_terminated_and_reclaimed(
    monkeypatch, ledger: Path
):
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "1")
    execution_id = _running_external_execution(ledger, progress_at=time.time() - 10)
    monkeypatch.setattr(executions, "_is_exact_external_worker", lambda _pid, _id: True)
    live_samples = iter((True, True, False, False))
    monkeypatch.setattr(
        executions, "_owner_is_live", lambda _pid, _started: next(live_samples)
    )
    terminated = []
    monkeypatch.setattr(
        "gateway.status.terminate_pid",
        lambda pid, **kwargs: terminated.append((pid, kwargs)),
    )

    assert executions.recover_interrupted_executions() == 1

    recovered = executions.get_execution(execution_id)
    assert recovered["status"] == "unknown"
    assert "progress lease expired" in recovered["error"]
    assert terminated and terminated[0][0] == 4242
    assert terminated[0][1] == {"force": True, "expected_start_time": 123456}


def test_fresh_or_unidentified_live_owner_is_not_reclaimed(monkeypatch, ledger: Path):
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "1")
    execution_id = _running_external_execution(ledger, progress_at=time.time())
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: True)
    monkeypatch.setattr(executions, "_is_exact_external_worker", lambda _pid, _id: False)

    assert executions.recover_interrupted_executions() == 0
    assert executions.get_execution(execution_id)["status"] == "running"

    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET progress_at=? WHERE id=?",
            (time.time() - 10, execution_id),
        )
    monkeypatch.setattr(executions, "_is_exact_external_worker", lambda _pid, _id: True)
    for timeout in ("0", "nan", "inf"):
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", timeout)
        assert executions.recover_interrupted_executions() == 0
        assert executions.get_execution(execution_id)["status"] == "running"

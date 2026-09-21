"""Regression coverage for safe retention windows in ``hermes kanban gc``."""

import argparse
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops
from hermes_cli.kanban_parser import _nonnegative_int


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    return home


def _args(event_days=30, log_days=30):
    return argparse.Namespace(
        event_retention_days=event_days,
        log_retention_days=log_days,
    )


def _old_done_event() -> str:
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="finished")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
            conn.execute("UPDATE task_events SET created_at = 0 WHERE task_id = ?", (task_id,))
    return task_id


def _event_count(task_id: str) -> int:
    with kbc.connect_closing() as conn:
        return int(conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0])


def _old_log() -> Path:
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "worker.log"
    path.write_text("worker output", encoding="utf-8")
    os.utime(path, (0, 0))
    return path


def _archived_workspace() -> Path:
    workspace = kb.workspaces_root() / "archived-task"
    workspace.mkdir(parents=True, exist_ok=True)
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="archived",
            workspace_kind="scratch",
            workspace_path=str(workspace),
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (task_id,))
    return workspace


def test_parser_rejects_negative_retention_days():
    with pytest.raises(argparse.ArgumentTypeError, match=">= 0"):
        _nonnegative_int("-1")
    assert _nonnegative_int("0") == 0


def test_negative_retention_is_rejected_before_any_gc_mutation(board, capsys):
    task_id = _old_done_event()
    log = _old_log()
    workspace = _archived_workspace()

    assert kanban_ops._cmd_gc(_args(event_days=-1, log_days=30)) == 2

    assert _event_count(task_id) > 0
    assert log.exists()
    assert workspace.exists()
    assert "non-negative" in capsys.readouterr().err


def test_zero_retention_disables_each_sweep(board):
    task_id = _old_done_event()
    log = _old_log()

    assert kanban_ops._cmd_gc(_args(event_days=0, log_days=0)) == 0

    assert _event_count(task_id) > 0
    assert log.exists()


def test_gc_helpers_reject_negative_seconds_without_mutation(board):
    task_id = _old_done_event()
    log = _old_log()

    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match="retention"):
            kb.gc_events(conn, older_than_seconds=-1)
    with pytest.raises(ValueError, match="retention"):
        kb.gc_worker_logs(older_than_seconds=-1)

    assert _event_count(task_id) > 0
    assert log.exists()


def test_slash_gc_rejects_negative_retention(board):
    from hermes_cli import kanban

    task_id = _old_done_event()
    log = _old_log()

    output = kanban.run_slash(
        "gc --event-retention-days -1 --log-retention-days 30"
    )

    assert "must be >= 0" in output
    assert _event_count(task_id) > 0
    assert log.exists()


def test_positive_retention_still_collects_old_events_and_logs(board):
    task_id = _old_done_event()
    log = _old_log()

    assert kanban_ops._cmd_gc(_args(event_days=1, log_days=1)) == 0

    assert _event_count(task_id) == 0
    assert not log.exists()

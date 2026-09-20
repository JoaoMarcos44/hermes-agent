"""Invariant: BLOB-typed text cells must not abort the dispatch pass (issue #116473).

A ``task_comments.body`` stored as BLOB comes back from sqlite3 as ``bytes``.
``check_respawn_guard`` ran the ``str`` PR-URL pattern straight over it, so
``re.search`` raised ``TypeError: cannot use a string pattern on a bytes-like
object`` — and because the guard runs per row with no isolation, one corrupt
comment aborted the whole ``dispatch_once`` pass: no card on the board
dispatched until the row was deleted by hand. The same BLOB-as-bytes shape
threatened every other raw read in the guard path (``last_failure_error``,
run ``outcome``/``error``/``metadata``, event ``kind``/``payload``).

The guard now coerces each cell through the shared ``_lossy_text`` helper
(U+FFFD for undecodable sequences), so a corrupt row degrades instead of
taking the pass down — while a genuine PR URL still guards.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_respawn_guard_survives_blob_cells(kanban_home):
    with kbc.connect() as conn:
        blob_comment = kb.create_task(conn, title="blob comment", assignee="coder")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (blob_comment,))
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at)"
            " VALUES (?, 'user', X'FFFEABCD00', 1000)",
            (blob_comment,),
        )
        blob_error = kb.create_task(conn, title="blob error", assignee="coder")
        conn.execute(
            "UPDATE tasks SET status = 'ready',"
            " last_failure_error = X'FFFEABCD00' WHERE id = ?",
            (blob_error,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at)"
            " VALUES (?, 'assigned', X'FFFEABCD00', 1000)",
            (blob_comment,),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, error,"
            " metadata, started_at, ended_at)"
            " VALUES (?, 'coder', 'closed', X'FFFEABCD00',"
            " X'FFFEABCD00', X'FFFEABCD00', 1000, 1001)",
            (blob_comment,),
        )
        conn.commit()

        assert kbd.check_respawn_guard(conn, blob_comment) is None
        assert kbd.check_respawn_guard(conn, blob_error) is None
        assert kbd._protocol_violation_streak(conn, blob_comment) == 0


def test_dispatch_pass_completes_with_blob_comment_and_still_guards_real_pr(
    kanban_home,
    all_assignees_spawnable,
):
    with kbc.connect() as conn:
        tainted = kb.create_task(conn, title="tainted", assignee="coder")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tainted,))
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at)"
            " VALUES (?, 'user', X'FFFEABCD00', 1000)",
            (tainted,),
        )
        pr_card = kb.create_task(conn, title="has pr", assignee="coder")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (pr_card,))
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at)"
            " VALUES (?, 'worker',"
            " 'opened https://github.com/acme/repo/pull/42 for review', ?)",
            (pr_card, int(time.time())),
        )
        conn.commit()

        assert kbd.check_respawn_guard(conn, pr_card) == "active_pr"
        # The whole pass must complete despite the BLOB comment — before the
        # fix this raised TypeError and dispatched nothing.
        result = kbd._dispatch_once_locked(conn, dry_run=True, reconcile_orphans=False)
        assert isinstance(result, kbd.DispatchResult)
        assert any(t == pr_card for t, _ in result.respawn_guarded)

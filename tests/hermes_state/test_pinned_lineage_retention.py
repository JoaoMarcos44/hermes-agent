"""Regression coverage for pinned compression lineages during maintenance."""

import time
from contextlib import closing

from hermes_state import SessionDB

_OLD_DAYS = 120


def _split_pinned_lineage(
    db: SessionDB, prefix: str, *, tip_open: bool = False, source: str = "cli",
) -> tuple[float, tuple[str, str, str]]:
    """Create root -> mid -> tip, then persist the legacy split-pin shape."""
    old = time.time() - _OLD_DAYS * 86400
    root, mid, tip = f"{prefix}-root", f"{prefix}-mid", f"{prefix}-tip"

    db.create_session(root, source=source)
    db.append_message(root, "user", "old root", timestamp=old)
    db.set_session_pinned(root, True)

    db.publish_compression_child(
        parent_session_id=root,
        child_session_id=mid,
        source=source,
        messages=[{"role": "user", "content": "old middle", "timestamp": old + 60}],
        require_compression_lease=False,
    )
    db.publish_compression_child(
        parent_session_id=mid,
        child_session_id=tip,
        source=source,
        messages=[{"role": "user", "content": "old tip", "timestamp": old + 120}],
        require_compression_lease=False,
    )
    if not tip_open:
        db.end_session(tip, "done")

    # Persist the pre-fix shape explicitly so this regression remains meaningful
    # even after compression-child flag inheritance lands independently.
    db._conn.execute(
        "UPDATE sessions SET pinned = CASE WHEN id = ? THEN 1 ELSE 0 END "
        "WHERE id IN (?, ?, ?)",
        (root, root, mid, tip),
    )
    for offset, sid in enumerate((root, mid, tip)):
        ts = old + offset * 60
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, last_activity_at = ?, "
            "ended_at = CASE WHEN ended_at IS NULL THEN NULL ELSE ? END WHERE id = ?",
            (ts, ts, ts + 10, sid),
        )
        db._conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (ts, sid))
    db._conn.commit()
    return old, (root, mid, tip)


def _old_branch(db: SessionDB, root: str, old: float) -> str:
    branch = f"{root}-branch"
    db.create_session(
        branch,
        source="cli",
        parent_session_id=root,
        model_config={"_branched_from": root},
    )
    db.append_message(branch, "user", "independent branch", timestamp=old)
    db.end_session(branch, "done")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, last_activity_at = ?, ended_at = ?, pinned = 0 "
        "WHERE id = ?",
        (old, old, old + 10, branch),
    )
    db._conn.commit()
    return branch


def test_prune_keeps_split_pinned_continuations_but_not_a_branch(tmp_path):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        old, lineage = _split_pinned_lineage(db, "keep")
        root, _, tip = lineage
        branch = _old_branch(db, root, old)

        assert [db.get_session(sid)["pinned"] for sid in lineage] == [1, 0, 0]
        assert {
            row["id"]
            for row in db.list_prune_candidates(older_than_days=90, whole_lineages=True)
        } == {branch}

        assert db.prune_sessions(older_than_days=90) == 1
        assert db.get_session(branch) is None
        assert all(db.get_session(sid) is not None for sid in lineage)

        # Explicit opt-in still means exactly that: pin protection is bypassed.
        assert db.prune_sessions(older_than_days=90, include_pinned=True) == 3
        assert all(db.get_session(sid) is None for sid in lineage)


def test_archive_paths_keep_existing_split_pinned_lineage(tmp_path):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _, lineage = _split_pinned_lineage(db, "archive")

        assert db.archive_sessions(older_than_days=90) == 0
        assert db.archive_stale_sessions(90) == 0
        assert [db.get_session(sid)["archived"] for sid in lineage] == [0, 0, 0]

        assert db.archive_stale_sessions(90, exclude_pinned=False) == 1
        assert [db.get_session(sid)["archived"] for sid in lineage] == [1, 1, 1]


def test_orphan_sweep_keeps_open_continuation_of_pinned_segment(tmp_path):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        _, lineage = _split_pinned_lineage(db, "sweep", tip_open=True)
        tip = lineage[-1]

        assert db.sweep_orphaned_sessions(
            max_idle_seconds=90 * 86400,
            sources=("cli",),
            exclude_pinned=True,
            respect_gateway_heartbeats=False,
        ) == []
        assert db.get_session(tip)["ended_at"] is None

        assert db.sweep_orphaned_sessions(
            max_idle_seconds=90 * 86400,
            sources=("cli",),
            exclude_pinned=False,
            respect_gateway_heartbeats=False,
        ) == [tip]
        assert db.get_session(tip)["end_reason"] == "startup_orphan_reap"

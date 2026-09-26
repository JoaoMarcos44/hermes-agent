"""Durable invariants for whole-lineage retention pruning.

These tests use a real SessionDB and real SQLite rows; no mocks or fake store
objects. They are intended to be cherry-picked on top of PR #123884.
"""

from contextlib import closing
import time

from hermes_state import SessionDB


_OLD = 120 * 86400


def _old_session(db: SessionDB, session_id: str, *, end_reason: str = "compression") -> float:
    at = time.time() - _OLD
    db.create_session(session_id, source="cli")
    db.append_message(session_id, "user", f"old:{session_id}", timestamp=at)
    db.end_session(session_id, end_reason)
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
        (at, at + 1, session_id),
    )
    db._conn.commit()
    return at


def test_whole_lineage_prune_distinguishes_continuations_from_forks(tmp_path):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        cases = {
            "continue": ("cli", {}),
            "branch": ("cli", {"_branched_from": "branch-root"}),
            "delegate": ("cli", {"_delegate_from": "delegate-root"}),
            "reset": ("cli", {"_reset_from": "reset-root"}),
            "tool": ("tool", {}),
        }

        for kind, (source, marker) in cases.items():
            root = f"{kind}-root"
            child = f"{kind}-child"
            _old_session(db, root)
            db.create_session(
                child,
                source=source,
                parent_session_id=root,
                model_config=marker or None,
            )

        candidates = {
            row["id"]
            for row in db.list_prune_candidates(
                older_than_days=90,
                whole_lineages=True,
            )
        }

        # Only the true compression continuation protects its old parent.
        assert "continue-root" not in candidates
        assert {
            "branch-root",
            "delegate-root",
            "reset-root",
            "tool-root",
        } <= candidates

        assert db.prune_sessions(older_than_days=90) == 4
        assert db.get_session("continue-root") is not None
        assert db.get_session("continue-child") is not None

        for kind in ("branch", "delegate", "reset", "tool"):
            assert db.get_session(f"{kind}-root") is None
            child = db.get_session(f"{kind}-child")
            assert child is not None
            assert child["parent_session_id"] is None


def test_auto_prune_live_turn_lease_spares_every_candidate_segment(tmp_path):
    with closing(SessionDB(tmp_path / "state.db")) as db:
        at = _old_session(db, "lease-root")
        db.create_session(
            "lease-tip",
            source="cli",
            parent_session_id="lease-root",
        )
        db.append_message("lease-tip", "user", "old tip", timestamp=at + 2)
        db.end_session("lease-tip", "done")
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (at + 2, at + 3, "lease-tip"),
        )
        db._conn.commit()

        holder = "12345:test-prune-lineage"
        assert db.try_acquire_session_turn_lease(
            "lease-tip",
            holder,
            ttl_seconds=300,
        )

        # The lease key resolves through compression parents to lease-root.
        # Guarding either candidate therefore guards the whole candidate chain.
        assert db.prune_sessions(
            older_than_days=90,
            exclude_active_write_guards=True,
        ) == 0
        assert db.get_session("lease-root") is not None
        assert db.get_session("lease-tip") is not None

        db.release_session_turn_lease("lease-tip", holder)

        assert db.prune_sessions(
            older_than_days=90,
            exclude_active_write_guards=True,
        ) == 2
        assert db.get_session("lease-root") is None
        assert db.get_session("lease-tip") is None

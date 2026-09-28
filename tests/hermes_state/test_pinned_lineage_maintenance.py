"""Pinned compression lineages are maintenance units (#126432)."""

import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _backdate(db: SessionDB, *session_ids: str) -> None:
    old = time.time() - 120 * 86400
    for session_id in session_ids:
        db._conn.execute(
            """UPDATE sessions
               SET started_at = ?, last_activity_at = ?,
                   ended_at = CASE WHEN ended_at IS NULL THEN NULL ELSE ? END
               WHERE id = ?""",
            (old, old, old, session_id),
        )
        db._conn.execute(
            "UPDATE messages SET timestamp = ? WHERE session_id = ?",
            (old, session_id),
        )
    db._conn.commit()


def _legacy_split(db: SessionDB, *, tip_open: bool = False) -> None:
    old = time.time() - 120 * 86400
    db.create_session("keep", source="cli")
    db.append_message("keep", "user", "early detail", timestamp=old)
    db.set_session_pinned("keep", True)
    db.publish_compression_child(
        parent_session_id="keep",
        child_session_id="keep-2",
        source="cli",
        messages=[{"role": "user", "content": "[summary]", "timestamp": old + 60}],
        require_compression_lease=False,
    )
    if not tip_open:
        db.end_session("keep-2", "done")

    # Reproduce a store written before compression children inherited the pin.
    # Keep this explicit even after the producer-side fix lands.
    db._conn.execute("UPDATE sessions SET pinned = 0 WHERE id = 'keep-2'")
    db._conn.commit()
    _backdate(db, "keep", "keep-2")
    assert db.get_session("keep")["pinned"] == 1
    assert db.get_session("keep-2")["pinned"] == 0


def test_prune_protects_legacy_split_without_changing_raw_selection(db):
    _legacy_split(db)

    # Raw selection is also used by export; keep its per-row contract.
    assert [row["id"] for row in db.list_prune_candidates(older_than_days=90)] == ["keep-2"]
    assert db.count_prune_matches(older_than_days=90, include_pinned=False) == 1
    assert db.count_prune_matches(older_than_days=90, include_pinned=True) == 2

    assert db.list_prune_candidates(older_than_days=90, whole_lineages=True) == []
    assert db.prune_sessions(older_than_days=90) == 0
    assert db.get_session("keep") is not None
    assert db.get_session("keep-2") is not None


def test_prune_include_pinned_still_deletes_the_whole_split(db):
    _legacy_split(db)

    assert db.prune_sessions(older_than_days=90, include_pinned=True) == 2
    assert db.get_session("keep") is None
    assert db.get_session("keep-2") is None


def test_archive_paths_protect_legacy_split_and_keep_the_opt_out(db):
    _legacy_split(db)

    assert db.list_prune_candidates(
        older_than_days=90, archived=False, lineage_tips_only=True
    ) == []
    assert db.archive_sessions(older_than_days=90) == 0
    assert db.archive_stale_sessions(90) == 0
    assert db.get_session("keep")["archived"] == 0
    assert db.get_session("keep-2")["archived"] == 0

    assert db.archive_stale_sessions(90, exclude_pinned=False) == 1
    assert db.get_session("keep")["archived"] == 1
    assert db.get_session("keep-2")["archived"] == 1


def test_orphan_sweep_protects_open_legacy_tip_and_keeps_the_opt_out(db):
    _legacy_split(db, tip_open=True)

    assert db.sweep_orphaned_sessions(
        max_idle_seconds=90 * 86400,
        sources=("cli",),
        exclude_pinned=True,
        respect_gateway_heartbeats=False,
    ) == []
    assert db.get_session("keep-2")["ended_at"] is None

    assert db.sweep_orphaned_sessions(
        max_idle_seconds=90 * 86400,
        sources=("cli",),
        exclude_pinned=False,
        respect_gateway_heartbeats=False,
    ) == ["keep-2"]


@pytest.mark.parametrize(
    ("marker", "source"),
    [
        ("_branched_from", "cli"),
        ("_delegate_from", "subagent"),
        ("_reset_from", "cli"),
        (None, "tool"),
    ],
)
def test_pin_protection_does_not_cross_independent_child_edges(db, marker, source):
    db.create_session("keep", source="cli")
    db.set_session_pinned("keep", True)
    db.end_session("keep", "compression")
    model_config = {marker: "keep"} if marker else None
    db.create_session(
        "independent",
        source=source,
        parent_session_id="keep",
        model_config=model_config,
    )
    db.append_message("independent", "user", "old")
    db.end_session("independent", "done")
    _backdate(db, "keep", "independent")

    assert db.prune_sessions(older_than_days=90) == 1
    assert db.get_session("keep") is not None
    assert db.get_session("independent") is None


def test_pin_protection_follows_continuation_with_inherited_foreign_marker(db):
    db.create_session("keep", source="cli")
    db.set_session_pinned("keep", True)
    db.end_session("keep", "compression")
    db.create_session(
        "continuation",
        source="cli",
        parent_session_id="keep",
        model_config={"_branched_from": "older-parent"},
    )
    db.append_message("continuation", "user", "old")
    db.end_session("continuation", "done")
    _backdate(db, "keep", "continuation")

    assert db.prune_sessions(older_than_days=90) == 0
    assert db.get_session("continuation") is not None

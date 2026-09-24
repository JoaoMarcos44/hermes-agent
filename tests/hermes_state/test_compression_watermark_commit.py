"""Watermark commit: concurrent appends survive in-place compaction (#75316).

The provider summary call is external and slow. Messages that arrive while it
runs must (a) persist immediately — appends are not fenced by the compression
lock — and (b) survive the commit: ``archive_and_compact(watermark=...)``
re-sequences every active row above the watermark after the compacted set
instead of archiving it. The commit is holder-fenced: a compression whose
lease was reclaimed cannot publish a stale compaction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_state import SessionCompressionInProgressError, SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    d = SessionDB(tmp_path / "state.db")
    d.create_session("sess1", source="test")
    return d


def _seed(db: SessionDB, n: int = 6) -> None:
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        db.append_message("sess1", role=role, content=f"turn {i}")


def _prefix_proof(db: SessionDB, messages=None):
    held = messages if messages is not None else db.get_messages_as_conversation("sess1", include_row_ids=True)
    proof = db.match_active_message_prefix_proof("sess1", held)
    assert proof is not None
    return proof


@contextmanager
def _rewrite_lease(db: SessionDB, holder: str):
    assert db.try_acquire_compression_lock("sess1", holder) is True
    try:
        yield
    finally:
        db.release_compression_lock("sess1", holder)


SUMMARY = [
    {"role": "user", "content": "[CONTEXT COMPACTION] summary of turns 0-5"},
    {"role": "assistant", "content": "Continuing from the summary."},
]


class TestWatermarkCommit:
    def test_concurrent_tail_survives_compaction(self, db: SessionDB) -> None:
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")
        # Simulate the slow summary window: two messages land after capture.
        db.append_message("sess1", role="user", content="mid-compression steer")
        db.append_message("sess1", role="assistant", content="mid-compression reply")

        count = db.archive_and_compact("sess1", SUMMARY, watermark=watermark)

        live = db.get_messages("sess1")
        contents = [r["content"] for r in live]
        assert contents == [
            "[CONTEXT COMPACTION] summary of turns 0-5",
            "Continuing from the summary.",
            "mid-compression steer",
            "mid-compression reply",
        ], "tail must follow the summary, in arrival order"
        assert count == 4

    def test_expected_prefix_derives_watermark_and_keeps_unseen_tail(self, db: SessionDB) -> None:
        _seed(db)
        held_ids, held_digest = _prefix_proof(db)
        holder = "prefix-worker"
        with _rewrite_lease(db, holder):
            # Appends are deliberately not blocked by the rewrite lease.
            db.append_message("sess1", role="user", content="foreign turn")
            db.archive_and_compact(
                "sess1", SUMMARY,
                expected_active_prefix_ids=held_ids,
                expected_active_prefix_digest=held_digest,
                lock_holder=holder,
            )

        assert [row["content"] for row in db.get_messages("sess1")] == [
            SUMMARY[0]["content"], SUMMARY[1]["content"], "foreign turn",
        ]

    @pytest.mark.parametrize("mutation", ["generation", "payload", "closed"])
    def test_expected_prefix_refuses_stale_source(self, db: SessionDB, mutation: str) -> None:
        _seed(db)
        held = db.get_messages_as_conversation("sess1", include_row_ids=True)
        held_ids, held_digest = _prefix_proof(db, held)
        holder = f"{mutation}-worker"
        with _rewrite_lease(db, holder):
            if mutation == "generation":
                db.archive_and_compact("sess1", SUMMARY, watermark=held_ids[-1])
            elif mutation == "payload":
                first = held[0]
                assert db.set_message_api_content(
                    "sess1", first["_row_id"], first["content"], "late provider-side context",
                ) == 1
            else:
                db.end_session("sess1", "compression")

            with pytest.raises(SessionCompressionInProgressError):
                db.archive_and_compact(
                    "sess1", SUMMARY, expected_active_prefix_ids=held_ids,
                    expected_active_prefix_digest=held_digest, lock_holder=holder,
                )

        if mutation == "payload":
            current = db.get_messages_as_conversation("sess1", include_row_ids=True)
            assert current[0]["api_content"] == "late provider-side context"

    def test_expected_prefix_requires_rewrite_lease(self, db: SessionDB) -> None:
        _seed(db, 2)
        held_ids, held_digest = _prefix_proof(db)
        with pytest.raises(ValueError, match="lock_holder"):
            db.archive_and_compact(
                "sess1", SUMMARY, expected_active_prefix_ids=held_ids,
                expected_active_prefix_digest=held_digest,
            )

    def test_live_platform_message_id_matches_replayed_message_id(self, db: SessionDB) -> None:
        user_id = db.append_message("sess1", "user", "gateway turn", platform_message_id="telegram-42")
        assistant_id = db.append_message("sess1", "assistant", "gateway reply")
        held = [
            {"role": "user", "content": "gateway turn", "platform_message_id": "telegram-42",
             "_row_id": user_id, "_db_persisted": True},
            {"role": "assistant", "content": "gateway reply", "_row_id": assistant_id, "_db_persisted": True},
        ]
        proof = db.match_active_message_prefix_proof("sess1", held)
        assert proof is not None and proof[0] == [user_id, assistant_id]
    @pytest.mark.parametrize("case", ["equal", "replaced-model-switch"])
    def test_unstamped_durable_identity_is_ambiguous(self, db: SessionDB, case: str) -> None:
        _seed(db, 2)
        held = db.get_messages_as_conversation("sess1", include_row_ids=True)
        if case == "equal":
            held.append({"role": "user", "content": "yes"})
            db.append_message("sess1", "user", "yes")
        else:
            old = "[System: The active model for this chat has changed to test/old.]"
            new = "[System: The active model for this chat has changed to test/new.]"
            db.append_message("sess1", "user", old, display_kind="model_switch")
            held.append({"role": "user", "content": new, "display_kind": "model_switch"})
            db.append_message("sess1", "user", new, display_kind="model_switch")
        assert db.match_active_message_prefix_proof("sess1", held) is None
    def test_unstamped_unmatched_suffix_remains_in_memory_tail(self, db: SessionDB) -> None:
        _seed(db, 2)
        held = db.get_messages_as_conversation("sess1", include_row_ids=True)
        held.append({"role": "user", "content": "fresh local turn"})

        proof = db.match_active_message_prefix_proof("sess1", held)

        assert proof is not None
        resolved, _digest = proof
        assert len(resolved) == 2

    @pytest.mark.parametrize("include_row_ids", [False, True])
    def test_prefix_rejects_stale_provider_sidecar(
        self, db: SessionDB, include_row_ids: bool,
    ) -> None:
        _seed(db, 2)
        held = db.get_messages_as_conversation("sess1", include_row_ids=include_row_ids)
        row_id = db.get_messages("sess1")[0]["id"]
        assert db.set_message_api_content(
            "sess1", row_id, held[0]["content"], "newer provider-side context",
        ) == 1

        assert db.match_active_message_prefix_proof("sess1", held) is None

    def test_tail_clone_preserves_every_column(self, db: SessionDB) -> None:
        """The pure-SQL clone must carry sidecar fields byte-exact."""
        _seed(db, 2)
        watermark = db.get_active_message_watermark("sess1")
        db.append_message(
            "sess1",
            role="assistant",
            content="tool caller",
            tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "terminal", "arguments": "{}"}}],
        )
        db.append_message(
            "sess1", role="tool", content="tool output",
            tool_call_id="c1", tool_name="terminal",
        )

        db.archive_and_compact("sess1", SUMMARY, watermark=watermark)

        live = db.get_messages("sess1")
        by_content = {r["content"]: r for r in live}
        caller = by_content["tool caller"]
        result = by_content["tool output"]
        parsed = caller["tool_calls"]
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        assert parsed and parsed[0]["id"] == "c1"
        assert result["tool_call_id"] == "c1"
        assert result["tool_name"] == "terminal"

    def test_conversation_load_is_correct_after_commit(self, db: SessionDB) -> None:
        """The live conversation projection sees summary + tail, in order."""
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")
        db.append_message("sess1", role="user", content="late arrival")

        db.archive_and_compact("sess1", SUMMARY, watermark=watermark)

        convo = db.get_messages_as_conversation("sess1")
        assert [m["content"] for m in convo] == [
            "[CONTEXT COMPACTION] summary of turns 0-5",
            "Continuing from the summary.",
            "late arrival",
        ]

    def test_no_tail_behaves_identically_to_legacy(self, db: SessionDB) -> None:
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")
        count = db.archive_and_compact("sess1", SUMMARY, watermark=watermark)
        assert count == 2
        assert [r["content"] for r in db.get_messages("sess1")] == [
            SUMMARY[0]["content"], SUMMARY[1]["content"],
        ]

    def test_none_watermark_preserves_historical_behavior(self, db: SessionDB) -> None:
        """watermark=None archives everything — the pre-#75316 contract."""
        _seed(db)
        db.append_message("sess1", role="user", content="gets archived")
        count = db.archive_and_compact("sess1", SUMMARY, watermark=None)
        assert count == 2
        contents = [r["content"] for r in db.get_messages("sess1")]
        assert "gets archived" not in contents

    def test_archived_rows_stay_recoverable(self, db: SessionDB) -> None:
        """Originals (snapshot AND tail source rows) survive as archived."""
        _seed(db, 4)
        watermark = db.get_active_message_watermark("sess1")
        db.append_message("sess1", role="user", content="tail row")
        db.archive_and_compact("sess1", SUMMARY, watermark=watermark)

        everything = db.get_messages("sess1", include_inactive=True)
        archived = [r for r in everything if not r["active"]]
        assert sum(1 for r in archived if r["content"] == "turn 0") == 1
        # The tail original is archived; its clone is the live copy.
        tail_rows = [r for r in everything if r["content"] == "tail row"]
        assert sorted(bool(r["active"]) for r in tail_rows) == [False, True]

    def test_session_counters_include_tail(self, db: SessionDB) -> None:
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")
        db.append_message(
            "sess1", role="assistant", content="tail with tools",
            tool_calls=[{"id": "t1", "type": "function",
                         "function": {"name": "x", "arguments": "{}"}}],
        )
        db.archive_and_compact("sess1", SUMMARY, watermark=watermark)
        info = db.get_session("sess1")
        assert info["message_count"] == 3
        assert info["tool_call_count"] == 1


class TestCommitFence:
    def test_commit_refused_when_lease_lost(self, db: SessionDB) -> None:
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")
        assert db.try_acquire_compression_lock("sess1", "worker-A") is True
        # Lease reclaimed by another writer while worker-A's summary ran.
        db.release_compression_lock("sess1", "worker-A")
        assert db.try_acquire_compression_lock("sess1", "worker-B") is True

        with pytest.raises(SessionCompressionInProgressError):
            db.archive_and_compact(
                "sess1", SUMMARY, watermark=watermark, lock_holder="worker-A"
            )
        # Nothing committed: original transcript intact.
        assert [r["content"] for r in db.get_messages("sess1")] == [
            f"turn {i}" for i in range(6)
        ]

    def test_commit_refused_when_lease_expired(self, db: SessionDB) -> None:
        _seed(db)
        assert db.try_acquire_compression_lock(
            "sess1", "worker-A", ttl_seconds=0.05
        ) is True
        time.sleep(0.1)
        with pytest.raises(SessionCompressionInProgressError):
            db.archive_and_compact("sess1", SUMMARY, lock_holder="worker-A")

    def test_commit_allowed_for_live_holder(self, db: SessionDB) -> None:
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")
        assert db.try_acquire_compression_lock("sess1", "worker-A") is True
        count = db.archive_and_compact(
            "sess1", SUMMARY, watermark=watermark, lock_holder="worker-A"
        )
        assert count == 2

    def test_refused_commit_rolls_back_atomically(self, db: SessionDB) -> None:
        """Failure injection: the fence raise must leave zero partial writes."""
        _seed(db)
        before = db.get_messages("sess1", include_inactive=True)
        with pytest.raises(SessionCompressionInProgressError):
            db.archive_and_compact("sess1", SUMMARY, lock_holder="never-held")
        after = db.get_messages("sess1", include_inactive=True)
        assert len(before) == len(after)
        assert all(r["active"] for r in after)


class TestConcurrentAppendDuringCompaction:
    def test_append_racing_the_commit_transaction(self, db: SessionDB) -> None:
        """An append serialized behind the commit lands AFTER it — never lost.

        SQLite's write lock serializes the two transactions; whichever side
        wins, the append must end up in the live transcript.
        """
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")

        barrier = threading.Barrier(2, timeout=10)
        append_err: list = []

        def _racer():
            barrier.wait()
            try:
                db.append_message("sess1", role="user", content="racer")
            except Exception as exc:  # pragma: no cover
                append_err.append(exc)

        t = threading.Thread(target=_racer, daemon=True)
        t.start()
        barrier.wait()
        db.archive_and_compact("sess1", SUMMARY, watermark=watermark)
        t.join(timeout=10)

        assert not append_err, f"append died during commit race: {append_err}"
        contents = [r["content"] for r in db.get_messages("sess1")]
        assert "racer" in contents, "racing append was lost"


class TestRotationPathWatermark:
    """Legacy (non-in-place) compression rotates to a child session —
    the concurrent tail must follow the rotation instead of stranding in
    the closed parent."""

    @pytest.mark.parametrize(
        "tail_content",
        [
            "mid-rotation steer",
            [
                {"type": "text", "text": "inspect this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AA=="},
                },
            ],
        ],
        ids=["text", "multimodal"],
    )
    def test_tail_clones_into_the_child(self, db: SessionDB, tail_content) -> None:
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")
        assert db.try_acquire_compression_lock("sess1", "rotator") is True
        db.append_message("sess1", role="user", content=tail_content)
        # Ceiling captured AFTER the foreign append, BEFORE the rotation
        # path's own pre-publish flush (which this test has none of).
        ceiling = db.get_active_message_watermark("sess1")

        db.publish_compression_child(
            parent_session_id="sess1",
            child_session_id="child1",
            source="test",
            messages=SUMMARY,
            compression_lock_holder="rotator",
            require_compression_lease=True,
            watermark=watermark,
            watermark_ceiling=ceiling,
        )

        child = db.get_messages_as_conversation("child1")
        assert [m["content"] for m in child] == [
            SUMMARY[0]["content"],
            SUMMARY[1]["content"],
            tail_content,
        ]
        model_history, display_history = db.get_resume_conversations("child1")
        visible_steers = [
            message
            for message in display_history
            if message.get("content") == tail_content
        ]
        assert len(visible_steers) == 1
        assert visible_steers[0]["_row_id"] == model_history[-1]["_row_id"]
        assert all(
            message.get("content") != tail_content
            for message in db.get_ancestor_display_prefix("child1")
        )
        info = db.get_session("child1")
        assert info["message_count"] == 3
        # Parent keeps its copy for lineage recovery; parent is closed.
        parent_info = db.get_session("sess1")
        assert parent_info["end_reason"] == "compression"

    def test_ceiling_excludes_the_rotators_own_flush(self, db: SessionDB) -> None:
        """Rows the rotation path flushes AFTER the ceiling (its own input
        transcript, already inside the handoff) must NOT be cloned."""
        _seed(db)
        watermark = db.get_active_message_watermark("sess1")
        assert db.try_acquire_compression_lock("sess1", "rotator") is True
        db.append_message("sess1", role="user", content="foreign steer")
        ceiling = db.get_active_message_watermark("sess1")
        # Simulates the #47202 pre-publish flush of the rotator's own input.
        db.append_message(
            "sess1", role="user", content="rotator's own flush",
            compression_lock_holder="rotator",
        )

        db.publish_compression_child(
            parent_session_id="sess1",
            child_session_id="child1",
            source="test",
            messages=SUMMARY,
            compression_lock_holder="rotator",
            require_compression_lease=True,
            watermark=watermark,
            watermark_ceiling=ceiling,
        )

        child_contents = [
            m["content"] for m in db.get_messages_as_conversation("child1")
        ]
        assert "foreign steer" in child_contents
        assert "rotator's own flush" not in child_contents

    def test_no_watermark_keeps_historical_rotation(self, db: SessionDB) -> None:
        _seed(db)
        assert db.try_acquire_compression_lock("sess1", "rotator") is True
        db.append_message("sess1", role="user", content="stranded either way")
        db.publish_compression_child(
            parent_session_id="sess1",
            child_session_id="child1",
            source="test",
            messages=SUMMARY,
            compression_lock_holder="rotator",
            require_compression_lease=True,
        )
        child = db.get_messages_as_conversation("child1")
        assert [m["content"] for m in child] == [
            SUMMARY[0]["content"], SUMMARY[1]["content"],
        ]

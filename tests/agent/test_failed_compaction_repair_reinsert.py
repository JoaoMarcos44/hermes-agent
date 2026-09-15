"""Regression for #111996: rematerialized compaction/repair copies must not re-INSERT.

When a fallback compaction rebuilds marker-swept dicts or alternation repair fuses
consecutive assistant rows, the append-only writer used to treat those copies as new
messages. One logical turn then accumulated N physical rows (same tool_call_id /
timestamp+content) and a fused junk-text + tool_calls dict landed beside the originals.

Invariant: adopting an already-ACTIVE logical identity does not grow the table;
a genuinely new turn still inserts; designed in-place compaction still republishes.
"""

from agent.agent_runtime_helpers import repair_message_sequence
from agent.context_compressor import _fresh_compaction_message_copy
from hermes_state import SessionDB


SESSION_ID = "s111996"
CALL_ID = "call_1b2db7a0565e4f92b0911631"
TS = 1_700_000_000.0


def _db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id=SESSION_ID, source="cli")
    return db


def _count(db, *, active_only=False):
    sql = "SELECT COUNT(*) FROM messages WHERE session_id = ?"
    if active_only:
        sql += " AND active = 1"
    return db._read_one(sql, (SESSION_ID,))[0]


def _unmarked_copies(rows):
    copies = []
    for row in rows:
        copy = {k: v for k, v in row.items() if not str(k).startswith("_")}
        copies.append(copy)
    return copies


def _tool_turn(base_ts=TS):
    return [
        {"role": "user", "content": "run it", "timestamp": base_ts},
        {
            "role": "assistant",
            "content": "",
            "timestamp": base_ts + 1,
            "finish_reason": "tool_calls",
            "tool_call_id": CALL_ID,
            "tool_calls": [
                {
                    "id": CALL_ID,
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": CALL_ID, "content": "output", "timestamp": base_ts + 2},
    ]


def test_rematerialized_unmarked_transcript_is_adopted(tmp_path):
    db = _db(tmp_path)
    try:
        db.append_messages_batch(SESSION_ID, _tool_turn())
        before = _count(db)
        assert before == 3
        db.append_messages_batch(SESSION_ID, _unmarked_copies(_tool_turn()))
        assert _count(db) == before
    finally:
        db.close()


def test_repeated_repack_does_not_grow(tmp_path):
    db = _db(tmp_path)
    try:
        db.append_messages_batch(SESSION_ID, _tool_turn())
        counts = []
        for _ in range(3):
            db.append_messages_batch(SESSION_ID, _unmarked_copies(_tool_turn()))
            counts.append(_count(db))
        assert counts == [3, 3, 3]
    finally:
        db.close()


def test_fresh_compaction_copy_flush_does_not_grow(tmp_path):
    db = _db(tmp_path)
    try:
        db.append_messages_batch(SESSION_ID, _tool_turn())
        copies = [_fresh_compaction_message_copy(row) for row in _tool_turn()]
        db.append_messages_batch(SESSION_ID, copies)
        assert _count(db) == 3
    finally:
        db.close()


def test_repaired_assistant_updates_survivor_and_archives_dropped(tmp_path):
    db = _db(tmp_path)
    try:
        rows = [
            {"role": "assistant", "content": "junk tic-tac-toe", "timestamp": TS},
            {
                "role": "assistant",
                "content": "",
                "timestamp": TS + 1,
                "tool_calls": [
                    {
                        "id": CALL_ID,
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": CALL_ID, "content": "output", "timestamp": TS + 2},
        ]
        db.append_messages_batch(SESSION_ID, rows)
        live = [{k: v for k, v in row.items()} for row in rows]
        repair_message_sequence(None, live)
        assert len(live) == 2
        assert live[0].get("tool_calls")
        db.append_messages_batch(SESSION_ID, live)
        assert _count(db) == 3
        assert _count(db, active_only=True) == 2
        assert "junk tic-tac-toe" in (live[0].get("content") or "")
    finally:
        db.close()


def test_new_timestamp_same_text_is_new_turn(tmp_path):
    db = _db(tmp_path)
    try:
        db.append_messages_batch(SESSION_ID, [{"role": "user", "content": "same", "timestamp": TS}])
        db.append_messages_batch(SESSION_ID, [{"role": "user", "content": "same", "timestamp": TS + 10}])
        assert _count(db) == 2
    finally:
        db.close()


def test_archive_and_compact_still_republishes_active_generation(tmp_path):
    db = _db(tmp_path)
    try:
        db.append_messages_batch(SESSION_ID, _tool_turn())
        compacted = [
            {"role": "user", "content": "summary of earlier turns", "timestamp": TS + 50},
            {"role": "assistant", "content": "ok", "timestamp": TS + 51},
        ]
        db.archive_and_compact(SESSION_ID, compacted)
        assert _count(db, active_only=True) == 2
        assert _count(db) >= 2
    finally:
        db.close()

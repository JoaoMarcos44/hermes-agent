"""Regression tests for failed-compaction transcript repair adoption."""
import sqlite3

from hermes_state import SessionDB
from agent.transcript_repair import resolve_and_repair_transcript_batch
from agent.agent_runtime_helpers import repair_message_sequence


def _db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s", source="cli")
    return db


def _count(db):
    return db._read_one("SELECT COUNT(*) FROM messages WHERE session_id = ?", ("s",))[0]


def test_rematerialized_unmarked_transcript_is_adopted(tmp_path):
    db = _db(tmp_path)
    rows = [{"role":"user","content":"q","timestamp":"1"}, {"role":"assistant","content":"a","timestamp":"2"}, {"role":"user","content":"r","timestamp":"3"}]
    db.append_messages_batch("s", rows)
    copies = [dict(row) for row in rows]
    for row in copies: row.pop("_row_id", None)
    db.append_messages_batch("s", copies)
    assert _count(db) == 3
    db.close()


def test_repeated_repack_does_not_grow(tmp_path):
    db = _db(tmp_path)
    rows = [{"role":"user","content":"q","timestamp":"1"}, {"role":"assistant","content":"a","timestamp":"2"}, {"role":"user","content":"r","timestamp":"3"}]
    db.append_messages_batch("s", rows)
    counts = []
    for _ in range(3):
        db.append_messages_batch("s", [{k:v for k,v in row.items() if not k.startswith("_")} for row in rows])
        counts.append(_count(db))
    assert counts == [3, 3, 3]
    db.close()


def test_repaired_assistant_updates_survivor_and_archives_dropped(tmp_path):
    db = _db(tmp_path)
    rows = [{"role":"assistant","content":"junk","timestamp":"1"}, {"role":"assistant","content":"tic","tool_calls":[{"id":"t1"}],"timestamp":"2"}]
    db.append_messages_batch("s", rows)
    messages = [dict(row) for row in rows]
    repair_message_sequence(None, messages)
    db.append_messages_batch("s", [messages[0]])
    assert _count(db) == 2
    assert messages[0]["content"] == "junk\ntic"
    db.close()


def test_new_timestamp_same_text_is_new_turn(tmp_path):
    db = _db(tmp_path)
    db.append_messages_batch("s", [{"role":"user","content":"same","timestamp":"1"}])
    db.append_messages_batch("s", [{"role":"user","content":"same","timestamp":"2"}])
    assert _count(db) == 2
    db.close()

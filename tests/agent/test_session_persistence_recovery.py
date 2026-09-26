"""Regression coverage for live-agent recovery after its durable session row disappears.

#123583: `_session_db_created` is an in-memory creation cache, not proof that
the row still exists.  A deleted/rebuilt store can therefore reject every later
message batch with a FOREIGN KEY error while the agent keeps the flag set.
"""

from __future__ import annotations

import os
import sqlite3
from unittest.mock import patch

import pytest

from hermes_state import SessionDB


def _make_agent(db, session_id="live-session", **kwargs):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        return AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
            save_trajectories=False,
            **kwargs,
        )


def test_flush_recreates_deleted_session_with_real_agent_metadata(tmp_path):
    """A stale created-flag recovers through AIAgent, not a synthetic state row."""
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _make_agent(
        db,
        platform="telegram",
        user_id="user-1",
        chat_id="chat-1",
        chat_type="dm",
        gateway_session_key="agent:main:telegram:dm:chat-1",
    )
    try:
        agent._ensure_db_session()
        assert agent._session_db_created is True
        assert db.delete_session(agent.session_id) is True
        assert agent._session_db_created is True  # stale by construction

        messages = [{"role": "user", "content": "after delete"}]
        assert agent._flush_messages_to_session_db(messages, []) is True

        row = db.get_session(agent.session_id)
        assert row is not None
        assert row["source"] == "telegram"
        assert row["session_key"] == "agent:main:telegram:dm:chat-1"
        assert row["chat_id"] == "chat-1"
        assert row["user_id"] == "user-1"
        assert [message["content"] for message in db.get_messages(agent.session_id)] == ["after delete"]
    finally:
        agent.close()
        db.close()


def test_raw_sessiondb_append_keeps_missing_parent_fk_contract(tmp_path):
    """Only the agent may reconstruct identity; raw store writes stay strict."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with pytest.raises(sqlite3.IntegrityError) as raised:
            db.append_message("missing-session", role="user", content="orphan")
        assert raised.value.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
        assert db.get_session("missing-session") is None
    finally:
        db.close()


def test_failed_session_creation_stops_before_batch_append(tmp_path, monkeypatch):
    """A parent-FK create failure must not be followed by a guaranteed-failing append."""
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _make_agent(db, session_id="child", parent_session_id="missing-parent")
    append_attempts = 0
    real_append = db.append_messages_batch

    def counted_append(*args, **kwargs):
        nonlocal append_attempts
        append_attempts += 1
        return real_append(*args, **kwargs)

    monkeypatch.setattr(db, "append_messages_batch", counted_append)
    try:
        assert agent._session_db_created is False
        assert agent._flush_messages_to_session_db(
            [{"role": "user", "content": "cannot attach"}], []
        ) is False
        assert append_attempts == 0
        assert db.get_session(agent.session_id) is None
    finally:
        agent.close()
        db.close()


def test_missing_session_retry_is_bounded_when_recreate_fails(tmp_path, monkeypatch):
    """One FK may trigger one rebuild; later flushes retry creation, not the doomed append."""
    db = SessionDB(db_path=tmp_path / "state.db")
    agent = _make_agent(db)
    agent._ensure_db_session()
    assert db.delete_session(agent.session_id) is True

    create_attempts = 0
    append_attempts = 0
    real_append = db.append_messages_batch

    def fail_create(*args, **kwargs):
        nonlocal create_attempts
        create_attempts += 1
        raise sqlite3.OperationalError("database is locked")

    def counted_append(*args, **kwargs):
        nonlocal append_attempts
        append_attempts += 1
        return real_append(*args, **kwargs)

    monkeypatch.setattr(db, "create_session", fail_create)
    monkeypatch.setattr(db, "append_messages_batch", counted_append)
    messages = [{"role": "user", "content": "pending"}]
    try:
        assert agent._flush_messages_to_session_db(messages, []) is False
        assert (append_attempts, create_attempts) == (1, 1)
        assert agent._session_db_created is False

        assert agent._flush_messages_to_session_db(messages, []) is False
        assert (append_attempts, create_attempts) == (1, 2)
    finally:
        agent.close()
        db.close()

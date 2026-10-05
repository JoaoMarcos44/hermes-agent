"""Housekeeping recovery keeps receiving transport and the existing conversation's lifecycle."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


@pytest.mark.parametrize("prune_route", [False, True])
def test_recovered_relay_route_retains_receiving_transport(tmp_path, monkeypatch, prune_route):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = GatewayConfig()
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", user_id="user",
                           delivered_via_upstream_relay=True)
    store = SessionStore(tmp_path / "sessions", cfg)
    entry = store.get_or_create_session(source)
    sid = entry.session_id
    store.append_to_transcript(sid, {"role": "user", "content": "already handled"})
    entry.updated_at = datetime.now() - timedelta(days=10)
    store._save()
    if prune_route:
        assert store.prune_old_entries(1) == 1
    store.close_all_db_handles()

    reopened = SessionStore(tmp_path / "sessions", cfg)
    # A cold database recovery has no live relay marker to borrow from the incoming source.
    cold_source = SessionSource.from_dict(source.to_dict())
    restored_entry = reopened.get_or_create_session(cold_source)
    assert restored_entry.session_id == sid
    reopened.update_session(restored_entry.session_key)
    restored_entry.updated_at = datetime.now() - timedelta(days=10)
    reopened._save()
    assert reopened.prune_old_entries(1) == 1
    restored_entry = reopened.get_or_create_session(cold_source)
    assert restored_entry.session_id == sid
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = cfg
    relay = SimpleNamespace(authorization_is_upstream=True)
    runner.adapters = {Platform.RELAY: relay}
    try:
        restored_source = runner._restored_source(restored_entry)
        assert restored_source.delivered_via_upstream_relay is True
        assert runner._delivery_adapter_for(restored_source) is relay
    finally:
        reopened.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, "session_meta", "user", "assistant"])
@pytest.mark.parametrize("route_state", ["pruned", "legacy"])
async def test_recovered_conversation_start_requires_agent_turn_evidence(
    tmp_path, monkeypatch, role, route_state,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = GatewayConfig()
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", user_id="user")
    store = SessionStore(tmp_path / "sessions", cfg)
    entry = store.get_or_create_session(source)
    sid = entry.session_id
    if role is not None:
        store.append_to_transcript(sid, {"role": role, "content": "existing row"})
    if role in {"user", "assistant"}:
        store._db_for_key(entry.session_key).touch_session_activity(sid, datetime.now().timestamp() + 1)
    entry.updated_at = datetime.now() - timedelta(days=10)
    store._save()
    if route_state == "pruned":
        assert store.prune_old_entries(1) == 1
    else:
        # Simulate the actual pre-field routing format, keeping its real durable transcript.
        legacy = entry.to_dict()
        legacy.pop("agent_turn_initialized")
        store._save_entry(entry.session_key, entry_data=legacy)
        store.close_all_db_handles()
        store = SessionStore(tmp_path / "sessions", cfg)
    recovered = store.get_or_create_session(source)
    assert recovered.session_id == sid
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = store
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    try:
        _, is_new = await runner._hmwa_open_session(recovered, recovered.session_key, source)
        assert is_new is (role not in {"user", "assistant"})
        assert runner.hooks.emit.await_count == int(is_new)
    finally:
        store.close_all_db_handles()

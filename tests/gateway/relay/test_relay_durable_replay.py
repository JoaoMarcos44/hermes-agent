"""Durable inbox replay continues an owned transcript and preserves ordinary inbound behavior."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.relay.durable_input import (
    event_from_replay_payload, gateway_input_owner, replay_event_payload,
)
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource, SessionStore, build_session_context
from gateway.turn_context import TurnContext


class _TurnObserved(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("relay_replay", [False, True])
@pytest.mark.parametrize("row_owned", [False, True])
@pytest.mark.parametrize("decorated", [False, True])
@pytest.mark.parametrize("message_id", [None, "input-1"])
async def test_owned_relay_replay_keeps_authored_input_and_only_resumes_existing_turn(
    tmp_path, monkeypatch, relay_replay, row_owned, decorated, message_id,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr("gateway.session._discord_tools_loaded", lambda: decorated)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    cfg = GatewayConfig(group_sessions_per_user=False)
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", user_id="user",
                           user_name="Ben" if decorated else None,
                           chat_type="group" if decorated else "dm",
                           delivered_via_upstream_relay=relay_replay)
    authored = "Complete the interrupted document comparison"
    doc = tmp_path / "input.txt"
    doc.write_text("reference input")
    original = MessageEvent(
        text=authored, source=source, message_id=message_id,
        channel_prompt="Original bound channel policy" if decorated else None,
        channel_context="[Earlier channel context]" if decorated else None,
        reply_to_message_id="quote-1" if decorated else None,
        reply_to_text="earlier reply context" if decorated else None,
        media_urls=[str(doc)], media_types=["text/plain"], media_text_inlined=[False],
    )
    if relay_replay:
        payload = replay_event_payload(original, buffer_id="durable-buffer")
        owner = payload["input_owner"]
        event = event_from_replay_payload(payload)
    else:
        owner = gateway_input_owner(original)
        event = original
    store = SessionStore(tmp_path / "sessions", cfg)
    entry = store.get_or_create_session(event.source)
    sid, key = entry.session_id, entry.session_key
    entry.agent_turn_initialized = True
    store._save()
    db = store._db_for_key(key)
    db.append_message(
        session_id=sid, role="user", content=authored if row_owned else "Previous unrelated request",
        timestamp=datetime.now().timestamp(),
        display_metadata={"gateway_input_owner": owner if row_owned else "different-input-owner"},
    )
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config, runner.session_store = cfg, store
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._set_session_env = lambda context: {}
    runner._clear_session_env = lambda tokens: None
    baseline_prompt = runner._pinned_session_context_prompt(
        build_session_context(event.source, cfg, entry), False, key,
    )
    runner._pinned_channel_inputs(key, event.channel_prompt, event.source, internal=False)
    runner._hmwa_acquire_turn_lease = AsyncMock()
    runner._mark_durable_active_turn = AsyncMock()
    runner._hmwa_run_session_hygiene = AsyncMock(side_effect=lambda event, source, entry, key, history, qk, gen: history)
    runner._hmwa_first_contact_notes = AsyncMock()
    runner._voice_channel_sidecar_note = lambda *args: None
    runner._bind_adapter_run_generation = lambda *args: None
    runner._delivery_adapter_for = lambda source: SimpleNamespace(interactive_resume=False)
    runner._consume_pending_native_image_paths = lambda key: []
    try:
        prepared, tokens = await runner._hmwa_prepare_turn(event, event.source, entry, key, key, 1)
        assert isinstance(prepared, runner._PreparedTurn)
        assert entry.session_id == sid
        assert prepared.context_prompt == baseline_prompt
        channel_prompt, turn_source = runner._pinned_channel_inputs(
            key, event.channel_prompt, event.source, internal=event.internal,
        )
        assert channel_prompt == original.channel_prompt
        assert turn_source.profile == original.source.profile
        if relay_replay or message_id:
            assert prepared.persistence_owner == owner
            assert gateway_input_owner(event) == owner
        else:
            # Ordinary keyless input still gets a fresh per-turn identity without relay pinning.
            assert prepared.persistence_owner != owner
            assert event._relay_input_owner is None
        owned_resume = relay_replay and row_owned
        if owned_resume:
            assert event.internal is True
            assert event.text == ""
            assert event.media_urls == []
            assert prepared.message_text == "", "the replay must not masquerade as a NEW authored input"
            assert prepared.persist_user_message in {None, ""}
            assert prepared.title_user_message is None
            assert prepared.persist_user_display_kind == "internal_notification"
            assert entry.resume_pending is True
        else:
            assert event.internal is False
            assert event.text == authored
            assert authored in prepared.message_text
            assert event.media_urls
            assert entry.resume_pending is False
        ctx = TurnContext(
            source=event.source, message=prepared.message_text, context_prompt=prepared.context_prompt,
            channel_prompt=channel_prompt,
            history=prepared.history, session_key=key, session_id=sid,
            persist_user_message=prepared.persist_user_message,
            persist_user_timestamp=prepared.persist_user_timestamp,
            persist_user_display_kind=prepared.persist_user_display_kind,
            persist_user_display_metadata={"gateway_input_owner": prepared.persistence_owner},
        )
        worker = TurnRunner(runner, ctx)
        persist_override, timestamp = worker._prepare_turn_message(prepared.history)
        if owned_resume:
            assert "CONTINUE the interrupted task to completion" in ctx.message
            assert "Address the user's NEW message" not in ctx.message
            assert authored not in ctx.message
            assert persist_override == ctx.message
        else:
            assert authored in ctx.message
            assert "gateway is now back online" not in ctx.message
        # Preparation and resume guidance leave the durable authored input and owning session alone.
        rows = db.get_messages(sid)
        assert sum(row["content"] == authored for row in rows) == int(row_owned)
        assert entry.active_turn_token is None
        # Exercise the real agent turn prologue and durable write, stopping before network I/O.
        from run_agent import AIAgent
        monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *args, **kwargs: None)
        observed = []
        def stop_before_network(agent, messages):
            observed.append(messages)
            raise _TurnObserved
        monkeypatch.setattr("agent.conversation_loop.begin_iteration", stop_before_network)
        agent = AIAgent(
            session_db=db, session_id=sid, model="test-model", api_key="test-key",
            base_url="http://127.0.0.1:1/v1", platform="discord", enabled_toolsets=[],
            quiet_mode=True, skip_memory=True, skip_context_files=True,
        )
        agent.compression_enabled = False
        try:
            with pytest.raises(_TurnObserved):
                worker._run_conversation_with_approval(
                    agent, prepared.history, None, persist_override, timestamp,
                )
            assert observed
            rows = db.get_messages(sid)
            if owned_resume:
                assert sum(row["content"] == authored for row in rows) == 1
                assert "CONTINUE the interrupted task to completion" in str(observed[0])
                assert any(row.get("display_kind") == "internal_notification" for row in rows)
            else:
                assert authored in str(observed[0])
            assert agent.session_id == sid
        finally:
            agent.close()
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("capsule_pending", [False, True])
async def test_startup_resume_yields_to_durable_capsule_owner(tmp_path, monkeypatch, capsule_pending):
    import asyncio
    from tests.gateway.relay.test_relay_inbound_admission_receipt import (
        _config, _live_adapter, _release_all, _text,
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = _config()
    store = SessionStore(tmp_path / "sessions", cfg)
    adapter, recorder = _live_adapter(store)
    event = _text("crash-owned", text="original pending input")
    entry = store.get_or_create_session(event.source)
    assert store.mark_resume_pending(entry.session_key, "restart_interrupted")
    if capsule_pending:
        assert await adapter._stage_durable_delivery(event, "pending-delivery", "buffer")
    await adapter._load_durable_resume_sessions(store)
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config, runner.session_store = cfg, store
    runner.adapters = {Platform.RELAY: adapter}
    runner._is_session_running = lambda key: False
    runner._resume_owner_authorized = lambda key, source: True
    runner._persist_active_agents = lambda: None
    runner._restart_loop_guard_config = lambda: (100, 3600, 600)
    runner._run_startup_resume_event = AsyncMock()
    runner._background_tasks = set()
    try:
        count = runner._schedule_resume_pending_sessions()
        if runner._background_tasks:
            await asyncio.wait_for(asyncio.gather(*tuple(runner._background_tasks)), 3)
        assert count == int(not capsule_pending)
        assert runner._run_startup_resume_event.await_count == int(not capsule_pending)
        if capsule_pending:
            assert store.get_relay_delivery_pending("pending-delivery") is not None
            assert entry.resume_pending is True
        else:
            resumed_event = runner._run_startup_resume_event.await_args.args[1]
            assert resumed_event.internal is True and resumed_event.text == ""
    finally:
        await _release_all(adapter, recorder)
        store.close_all_db_handles()

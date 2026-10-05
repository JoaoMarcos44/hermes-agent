"""Each durable input owner keeps its authored input in an independent turn."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import merge_pending_message_event
from tests.gateway.relay.test_relay_component_queue import _event, _idle
from tests.gateway.relay.test_relay_interactive import _adapter
from tests.gateway.test_queue_command import _make_runner, _make_source, _session_entry


def _authored(event):
    return event._relay_input_owner, event.text, tuple(event.media_urls)


@pytest.mark.parametrize("durable", [False, True], ids=["ordinary", "durable"])
@pytest.mark.parametrize("lane", [
    "base_queue", "base_interrupt", "base_queue_photo", "base_interrupt_photo",
    "batch", "pending_text", "pending_photo", "runner_photo", "runner_grace",
])
@pytest.mark.asyncio
async def test_authored_input_owners_survive_queue_and_batch_boundaries(durable, lane):
    """Journal inputs stay discrete; normal split text and photo bursts still combine."""
    adapter, _stub = _adapter(platform="discord")
    adapter._busy_text_mode = "queue" if "queue" in lane else "interrupt"
    adapter._busy_text_debounce_seconds = adapter._busy_text_hard_cap_seconds = 30
    adapter._text_batch_delay_seconds = adapter._text_batch_split_delay_seconds = 30
    incoming = [_event(name, interaction=False, photo="photo" in lane) for name in ("first", "second")]
    for event in incoming:
        if lane.startswith("runner_"):
            event.source = _make_source()
        event._relay_durable_pending = durable
        event._relay_input_owner = event.message_id if durable else None
    expected = [_authored(event) for event in incoming]
    received = []
    release, entered = asyncio.Event(), asyncio.Event()
    occupying = _event("occupying", interaction=False)

    async def record(event):
        if event is occupying:
            entered.set()
            await release.wait()
        else:
            received.append(_authored(event))

    adapter.set_message_handler(record)
    key = adapter._event_session_key(incoming[0])
    try:
        if lane.startswith("base_") or lane == "batch":
            await adapter.handle_message(occupying)
            await asyncio.wait_for(entered.wait(), 3)
            if lane == "batch":
                for event in incoming:
                    adapter._enqueue_text_event(event)
                for batch_key in list(adapter._pending_text_batches):
                    adapter._pending_text_batch_tasks[batch_key].cancel()
                    await adapter._flush_text_batch_now(batch_key)
            else:
                for event in incoming:
                    await adapter.handle_message(event)
            admitted = [event._gateway_accepted for event in incoming]
            release.set()
            await _idle(adapter)
            if durable:
                for event, accepted in zip(incoming, admitted):
                    if not accepted:
                        await adapter.handle_message(event)
                        await _idle(adapter)
        else:
            if lane.startswith("runner_"):
                runner, _ = _make_runner(_session_entry())
                runner._delivery_adapter_for = lambda source: adapter
                for event in incoming:
                    if lane == "runner_grace":
                        runner._hm_merge_pending_for_source(event.source, key, event, merge_text=True)
                    else:
                        runner._queue_or_replace_pending_event(key, event)
                head = adapter._pending_messages.pop(key)
                received.append(_authored(head))
                while (next_event := runner._promote_queued_event(key, adapter, None)) is not None:
                    received.append(_authored(next_event))
            else:
                for event in incoming:
                    merge_pending_message_event(adapter._pending_messages, key, event, merge_text=True)
                received.append(_authored(adapter._pending_messages.pop(key)))
                if durable:
                    for event in incoming:
                        if not any(owner == event._relay_input_owner for owner, _, _ in received):
                            merge_pending_message_event(adapter._pending_messages, key, event, merge_text=True)
                            received.append(_authored(adapter._pending_messages.pop(key)))
        if durable:
            assert received == expected, "one persisted user row must own exactly one capsule"
        else:
            assert len(received) == 1
            assert all(text in received[0][1] for _, text, _ in expected)
            assert received[0][2] == tuple(media for _, _, urls in expected for media in urls)
    finally:
        release.set()
        batch_tasks = list(adapter._pending_text_batch_tasks.values())
        for task in batch_tasks:
            task.cancel()
        await asyncio.gather(*batch_tasks, return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.parametrize("durable", [False, True], ids=["ordinary", "durable"])
@pytest.mark.parametrize("lane,mode", [
    ("busy", "steer"), ("busy", "interrupt"),
    ("priority", "steer"), ("priority", "interrupt"), ("explicit", "steer"),
])
@pytest.mark.asyncio
async def test_durable_ordinary_input_queues_before_steering_another_owner(monkeypatch, durable, mode, lane):
    runner, adapter = _make_runner(_session_entry())
    event = _event("incoming", interaction=False)
    event.source = _make_source()
    event._relay_durable_pending = durable
    event._relay_input_owner = "incoming-owner" if durable else None
    key = runner._session_key_for_source(event.source)
    runner._session_state(key).turn.agent = MagicMock(spec=[])
    if lane == "explicit":
        event.text = "/steer action incoming"
        event.allow_gateway_control = True
        runner._session_state(key).turn.agent = MagicMock(spec=["steer"])
        runner._steer_running_agent = MagicMock(return_value=True)
        runner._fold_into_running_turn = MagicMock()
    runner._effective_busy_input_mode = lambda source: mode
    runner._effective_busy_text_mode = lambda source: "interrupt"
    runner._is_user_authorized_for_source = lambda source: True
    runner._admit_bot_message_for_source = lambda source: True
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._agent_has_active_subagents = lambda agent: False
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._hm_busy_slash_or_photo = AsyncMock(return_value=(False, None))
    runner._hm_busy_telegram_grace_queue = lambda *args: False
    runner._hm_busy_steer = MagicMock()
    runner._hm_busy_interrupt = AsyncMock()
    resolve = runner._resolve_busy_steer_or_redirect = AsyncMock(return_value=SimpleNamespace(
        effective_mode=mode, redirected=mode == "interrupt", steered=mode == "steer",
    ))
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    if lane == "busy":
        assert await runner._handle_active_session_busy_message(event, key) is True
        assert resolve.await_count == (0 if durable else 1)
    elif lane == "priority":
        await runner._hm_handle_running_session_message(event, event.source, key)
        assert runner._hm_busy_steer.call_count == (0 if durable or mode != "steer" else 1)
        assert runner._hm_busy_interrupt.await_count == (0 if durable or mode != "interrupt" else 1)
    else:
        await runner._busy_steer_command(event, key, event.source)
        assert runner._steer_running_agent.call_count == (0 if durable else 1)
        assert runner._fold_into_running_turn.call_count == (0 if durable else 1)
    if durable:
        queued = adapter._pending_messages[key]
        if lane != "explicit":
            assert queued is event
        assert queued._relay_durable_pending
        assert _authored(queued) == ("incoming-owner", "action incoming", ())
    else:
        assert key not in adapter._pending_messages

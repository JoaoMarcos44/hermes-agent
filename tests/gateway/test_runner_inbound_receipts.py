"""A busy acknowledgement must not settle work retained only in runner RAM."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.inbound_receipt import begin_inbound, finish_inbound
from gateway.run import _AGENT_PENDING_SENTINEL
from tests.gateway.test_queue_command import _make_runner, _make_source, _session_entry


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["queue", "steer_pending", "steer_missing", "photo_interaction", "orphan", "startup", "recursive", "recursive_durable", "recursive_prepare_cancel"])
async def test_runner_ram_followup_waits_for_its_successor(route):
    runner, adapter = _make_runner(_session_entry())
    source = _make_source()
    key = runner._session_key_for_source(source)
    receipt = asyncio.get_running_loop().create_future()
    event = MessageEvent(text="follow-up", source=source, message_id="follow-up", _inbound_receipts=[receipt])
    begin_inbound(event)

    if route == "queue":
        event.text = "/queue follow-up"
        result = await runner._busy_queue_command(event, key, source)
        assert "queued" in result.lower()
        successor = adapter._pending_messages[key]
    elif route.startswith("steer_"):
        event.text = "/steer follow-up"
        runner._session_state(key).turn.agent = (
            _AGENT_PENDING_SENTINEL if route == "steer_pending" else MagicMock(spec=[])
        )
        result = await runner._busy_steer_command(event, key, source)
        assert "queued" in result.lower()
        successor = adapter._pending_messages[key]
    elif route == "photo_interaction":
        photo = MessageEvent(text="photo", message_type=MessageType.PHOTO, source=source, message_id="photo")
        runner._enqueue_fifo(key, photo, adapter)
        event.metadata = {"discord_interaction_id": "follow-up"}
        runner._queue_or_replace_pending_event(key, event)
        assert adapter._pending_messages[key] is photo
        assert photo.text == "photo"
        successor = runner._overflow_queue(key)[0]
        assert successor.metadata["discord_interaction_id"] == "follow-up"
    elif route == "orphan":
        orphan_receipt = asyncio.get_running_loop().create_future()
        orphan = MessageEvent(text="orphan", source=source, _inbound_receipts=[orphan_receipt])
        runner._session_state(key).conversation.queued_events.append(orphan)
        successor, _, _ = runner._hm_rescue_orphaned_fifo(event, source, False, key)
        assert successor is orphan
        finish_inbound(successor, consumed=True)
        assert orphan_receipt.result() is True
        successor = adapter._pending_messages[key]
    elif route == "startup":
        runner._queue_startup_restore_event(event)
        successor = runner._startup_restore_queue[0]
    else:
        if route == "recursive_durable":
            event._relay_durable_pending = True
            event._relay_input_owner = "durable-input-owner"
        runner._enqueue_fifo(key, event, adapter)
        successor = adapter._pending_messages.pop(key)
        entered, release = asyncio.Event(), asyncio.Event()

        async def run_agent(**kwargs):
            if route == "recursive_durable":
                assert kwargs["persist_user_display_metadata"]["gateway_input_owner"] == "durable-input-owner"
            entered.set()
            await release.wait()
            return {"final_response": "follow-up done", "messages": []}

        runner._run_agent = run_agent
        runner._run_agent_deliver_first_response = AsyncMock()
        async def prepare(**kwargs):
            entered.set()
            await release.wait()
            return "follow-up"

        runner._prepare_profile_scoped_inbound_message_text = (
            prepare if route == "recursive_prepare_cancel" else AsyncMock(return_value="follow-up")
        )
        runner._pinned_channel_inputs = lambda key, prompt, src, **kwargs: (prompt, src)
        runner._persist_prompt_pins = AsyncMock()
        runner._refresh_agent_cache_message_count = AsyncMock()
        runner._intake_adapter_for = lambda source: None
        ctx = SimpleNamespace(source=source, session_id="session", session_key=key, run_generation=1,
                              _interrupt_depth=0, history=[], _status_thread_metadata={},
                              context_prompt="", channel_prompt=None, result_holder=[None])
        task = asyncio.create_task(runner._run_agent_queued_followup(
            ctx, adapter, successor.text, successor, "old", {"final_response": "old", "messages": []}, None,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not receipt.done()
            if route == "recursive_prepare_cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert receipt.result() is False
            else:
                release.set()
                await asyncio.wait_for(task, 3)
                assert receipt.result() is True
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return

    # The acknowledgement handler completed, but its work is still parked for another turn.
    finish_inbound(event, consumed=True)
    assert not receipt.done()
    begin_inbound(successor)
    finish_inbound(successor, consumed=True)
    assert receipt.result() is True

    # A completed ordinary command has no successor and is settled immediately.
    command_receipt = asyncio.get_running_loop().create_future()
    command = MessageEvent(text="/status", source=source, _inbound_receipts=[command_receipt])
    begin_inbound(command)
    finish_inbound(command, consumed=True)
    assert command_receipt.result() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["explicit", "priority", "redirect", "recursion_cap", "rewritten"])
@pytest.mark.parametrize("consumed", [True, False])
async def test_runner_steer_receipt_follows_active_turn(route, consumed):
    runner, _ = _make_runner(_session_entry())
    source = _make_source()
    key = runner._session_key_for_source(source)

    class Agent:
        def steer(self, text):
            return True

        redirect = steer

    agent = Agent()
    opening = MessageEvent(text="opening", source=source)
    begin_inbound(opening)
    turn = runner._session_state(key).turn
    turn.agent = agent
    turn.event = replace(opening, text="hook-rewritten opening") if route == "rewritten" else opening
    receipt = asyncio.get_running_loop().create_future()
    event = MessageEvent(text="/steer correction" if route == "explicit" else "correction", source=source, _inbound_receipts=[receipt])
    begin_inbound(event)
    if route == "explicit":
        await runner._busy_steer_command(event, key, source)
    elif route == "priority":
        runner._hm_busy_steer(event, agent, key)
    else:
        assert runner._redirect_active_turn(agent, event.text, key, event)

    finish_inbound(event, consumed=True)
    assert not receipt.done()
    if route == "recursion_cap":
        adapter = runner._delivery_adapter_for(source)
        ctx = SimpleNamespace(source=source, session_id="session", session_key=key, run_generation=1,
                              _interrupt_depth=runner._MAX_INTERRUPT_DEPTH, history=[],
                              _status_thread_metadata={}, result_holder=[None])
        await runner._run_agent_queued_followup(
            ctx, adapter, "leftover correction", None, "old", {"final_response": "old"}, None,
        )
        finish_inbound(opening, consumed=True)
        assert not receipt.done()
        queued = runner._delivery_adapter_for(source)._pending_messages.pop(key)
        begin_inbound(queued)
        finish_inbound(queued, consumed=consumed)
        assert receipt.result() is consumed
        return
    finish_inbound(opening, consumed=consumed)
    assert receipt.result() is consumed

"""RAM-only relay custody stays connector-owned when durable inbox storage is unavailable."""

import asyncio

import pytest

from gateway.relay.ws_transport import WebSocketRelayTransport
from gateway.session import SessionStore
from tests.gateway.relay.test_relay_inbound_admission_receipt import (
    _config, _interaction, _live_adapter, _release_all, _text,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["passthrough", "normalized"])
@pytest.mark.parametrize("busy", [False, True])
async def test_ram_custody_remains_replayable_when_inbox_unavailable(tmp_path, monkeypatch, lane, busy):
    monkeypatch.setattr(SessionStore, "stage_relay_delivery", lambda *args: False)
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)
    if busy:
        await adapter.handle_message(_text("occupier"))
        await asyncio.wait_for(recorder.entered.wait(), 3)

    async def deliver(target):
        if lane == "passthrough":
            await target._on_passthrough(_interaction(), "custody-buffer")
        else:
            transport = object.__new__(WebSocketRelayTransport)
            transport._inbound = target._on_inbound
            transport._send_inbound_ack = target._transport.ack_inbound
            await transport._on_inbound({
                "bufferId": "custody-buffer",
                "event": {
                    "text": "followup", "message_id": "custody-action",
                    "source": _text("custody-action").source.to_dict(),
                },
            })

    await deliver(adapter)
    try:
        assert adapter._transport.acked_buffer_ids == [], "RAM admission cannot ACK the buffer"
    finally:
        # Simulate process loss: do not release the handler or drain a queued input during cleanup.
        adapter._pending_messages.clear()
        for state in adapter._text_debounce_store().values():
            state.cancel_timer()
        adapter._text_debounce_store().clear()
        await adapter.cancel_background_tasks()
        await adapter._cancel_delivery_settlements()
    store.close_all_db_handles()

    reopened = SessionStore(tmp_path, _config())
    restarted, completed = _live_adapter(reopened)
    completed.release.set()
    acked = asyncio.Event()
    original_ack = restarted._transport.ack_inbound

    async def ack(buffer_id):
        await original_ack(buffer_id)
        acked.set()

    restarted._transport.ack_inbound = ack
    await deliver(restarted)
    await asyncio.wait_for(acked.wait(), 3)
    assert len(completed.retained) == 1
    await deliver(restarted)
    assert len(completed.retained) == 1, "completed replay must not run twice"
    assert restarted._transport.acked_buffer_ids == ["custody-buffer", "custody-buffer"]
    await _release_all(restarted, completed)
    await restarted._cancel_delivery_settlements()
    reopened.close_all_db_handles()


@pytest.mark.asyncio
async def test_settlement_storage_does_not_block_unrelated_loop_work(tmp_path, monkeypatch):
    """Block the real store lock and prove an unrelated loop callback can release it."""
    import threading

    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)
    recorder.release.set()
    entered = threading.Event()
    release = threading.Event()
    original = store.is_relay_delivery_settled

    def contended(key):
        entered.set()
        assert release.wait(3), "event loop stalled behind synchronous settlement I/O"
        return original(key)

    monkeypatch.setattr(store, "is_relay_delivery_settled", contended)
    loop = asyncio.get_running_loop()
    loop.call_soon(release.set)
    await adapter._on_passthrough(_interaction(), "contended-buffer")
    assert entered.is_set()
    await _release_all(adapter, recorder)
    await adapter._cancel_delivery_settlements()
    store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_count", [1, 513], ids=["one", "beyond-completed-cache"])
async def test_completed_work_retries_only_receipt_when_storage_recovers(tmp_path, monkeypatch, completed_count):
    import sqlite3

    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)
    recorder.release.set()
    db = store._routing_db
    monkeypatch.setattr(store, "stage_relay_delivery", lambda *args: False)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("temporary receipt write failure")

    with monkeypatch.context() as failure:
        failure.setattr(db, "set_relay_delivery_receipt", unavailable)
        for index in range(completed_count):
            buffer_id = "retry-receipt" if index == 0 else f"later-{index}"
            await adapter._on_passthrough(_interaction(interaction_id=f"press-{index}"), buffer_id)
            await asyncio.gather(*tuple(adapter._settlement_tasks))
        assert len(adapter._seen_inbound) <= adapter._SEEN_INBOUND_MAX
        assert adapter._transport.acked_buffer_ids == []
        assert not store.is_relay_delivery_settled("passthrough_buffer:retry-receipt")
        await adapter._on_passthrough(_interaction(), "retry-receipt")
        assert len(recorder.retained) == completed_count, "cache eviction cannot rerun unsettled effects"
        assert adapter._transport.acked_buffer_ids == []

    await adapter._on_passthrough(_interaction(), "retry-receipt")
    assert len(recorder.retained) == completed_count
    assert adapter._transport.acked_buffer_ids == ["retry-receipt"]
    assert store.is_relay_delivery_settled("passthrough_buffer:retry-receipt")
    await _release_all(adapter, recorder)
    await adapter._cancel_delivery_settlements()
    store.close_all_db_handles()

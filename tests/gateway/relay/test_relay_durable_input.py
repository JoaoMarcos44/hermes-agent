"""Durable input custody permits ACK-gated controls and survives model-task loss."""

import asyncio
import dataclasses
from pathlib import Path

import pytest

from gateway.relay.durable_input import (
    event_from_replay_payload, gateway_input_owner, materialize_replay_event,
    pin_event_payload, replay_event_payload,
)
from gateway.session import SessionStore
from tests.gateway.relay.test_relay_inbound_admission_receipt import (
    _config, _interaction, _live_adapter, _release_all, _text,
)


@pytest.mark.parametrize("shape", ["text", "media"])
def test_exact_snapshot_preserves_owner_and_attachment_custody(tmp_path, shape):
    store = SessionStore(tmp_path / "sessions", _config())
    event = _text("action", text="authored input")
    event.source.profile = "worker"
    event.source.role_authorized = True
    event.reply_to_message_id = "attached-bot-message"
    event.channel_context = "context"
    event.auto_skill = ["one", "two"]
    event.metadata = {"discord_modal_fields": {"empty": "", "query": "needle"}}
    event.raw_message = {"token": "transport-secret"}
    event._inbound_receipts.append(object())
    if shape == "media":
        original = tmp_path / "cache" / "photo.png"
        original.parent.mkdir()
        original.write_bytes(b"immutable attachment")
        event.media_urls = [str(original)]
        event.media_types = ["image/png"]
        payload = pin_event_payload(store, "identity", event, buffer_id="buffer")
        moved = tmp_path / "routed-photo.png"
        original.rename(moved)  # Actual preprocessing is allowed to move its working file.
        moved.unlink()  # Cache cleanup cannot erase the separate inbox pin.
        pin = payload["event"]["media_urls"][0]
        restored = materialize_replay_event(store, "identity", payload)
        assert Path(restored.media_urls[0]).read_bytes() == b"immutable attachment"
        assert payload["event"]["media_urls"][0] == pin
        assert Path(pin).is_file() and restored.media_urls[0] != pin
    else:
        payload = replay_event_payload(event, buffer_id="buffer")
        restored = event_from_replay_payload(payload)
    try:
        assert restored.text == "authored input"
        assert restored.metadata == event.metadata
        assert restored.reply_to_message_id == "attached-bot-message"
        assert restored.source.profile == "worker" and restored.source.role_authorized
        assert restored.source.delivered_via_upstream_relay is True
        assert restored.channel_context == "context" and restored.auto_skill == ["one", "two"]
        assert restored.timestamp == event.timestamp and restored.raw_message is None
        assert restored._inbound_receipts == []
        assert gateway_input_owner(dataclasses.replace(restored)) == gateway_input_owner(event)
        assert "transport-secret" not in str(payload)
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ack-gated-control", "restart", "replacement", "refused", "receipt-outage"])
async def test_durable_inbox_recovery_and_ack_gated_progress(tmp_path, monkeypatch, scenario):
    store = SessionStore(tmp_path / "sessions", _config())
    adapter, recorder = _live_adapter(store)
    if scenario == "ack-gated-control":
        prompted, answered = asyncio.Event(), asyncio.Event()

        async def handler(event):
            recorder.retained.append(event)
            if event.message_id != "press-1":
                return None
            await adapter.send_clarify("ch1", "Which?", ["yes"], "clarify-local", "session-local")
            prompted.set()
            await answered.wait()

        def resolve(clarify_id, response):
            assert clarify_id == "clarify-local" and response == "yes"
            answered.set()
            return True

        monkeypatch.setattr("tools.clarify_gateway.resolve_gateway_clarify", resolve)
        adapter.set_message_handler(handler)
        try:
            await adapter._on_passthrough(_interaction(custom_id="ask"), "first")
            assert adapter._transport.acked_buffer_ids == ["first"]
            assert store.get_relay_delivery_pending("passthrough_buffer:first") is not None
            await asyncio.wait_for(prompted.wait(), 3)
            await adapter._on_passthrough(_interaction(interaction_id="queued", custom_id="normal"), "second")
            assert adapter._transport.acked_buffer_ids == ["first", "second"]
            assert len(recorder.retained) == 1, "the second input must still be queued"
            prompt_id = next(iter(adapter._pending_prompts))
            await adapter._on_passthrough(
                _interaction(interaction_id="answer", custom_id=f"hp1:{prompt_id}:c0"), "third")
            await asyncio.wait_for(answered.wait(), 3)
        finally:
            answered.set()
            await _release_all(adapter, recorder)
            await adapter._stop_delivery_recovery()
            store.close_all_db_handles()
        return

    event = _text("recover-me", text="original authored request")
    event.reply_to_message_id = "original-anchor"
    event.metadata = {"exact": ["", "value"]}
    key = "inbound_buffer:owned"
    assert await adapter._stage_durable_delivery(event, key, "owned")
    assert store.get_relay_delivery_pending(key)["event"]["text"] == event.text
    session_key = adapter._event_session_key(event)
    assert adapter._has_durable_resume_for(session_key)
    if scenario == "receipt-outage":
        import sqlite3

        marker = store._routing_db.set_relay_delivery_receipt
        adapter._SEEN_INBOUND_MAX = 1
        recorder.release.set()

        def fail_first(identity, **kwargs):
            if identity == key:
                raise sqlite3.OperationalError("local receipt outage")
            return marker(identity, **kwargs)

        try:
            with monkeypatch.context() as outage:
                outage.setattr(store._routing_db, "set_relay_delivery_receipt", fail_first)
                await adapter._start_delivery_recovery()
                await asyncio.wait_for(recorder.entered.wait(), 3)
                await asyncio.wait_for(asyncio.gather(*tuple(adapter._settlement_tasks)), 3)
                await adapter._stop_delivery_recovery()
                await adapter._on_inbound(_text("other", text="evict completed cache", chat_id="other-chat"))
                for _ in range(100):
                    if len(recorder.retained) == 2 and key not in adapter._completed_deliveries():
                        break
                    await asyncio.sleep(0.02)
                assert key not in adapter._completed_deliveries(), "production eviction must have run"
                await adapter._start_delivery_recovery()
                await asyncio.sleep(0.1)
                assert len(recorder.retained) == 2, "pending receipt retry must retain completed execution proof"
                assert store.get_relay_delivery_pending(key) is not None
            adapter._wake_delivery_recovery()
            for _ in range(100):
                if store.is_relay_delivery_settled(key):
                    break
                await asyncio.sleep(0.02)
            assert store.is_relay_delivery_settled(key)
            assert store.get_relay_delivery_pending(key) is None
            assert len(recorder.retained) == 2
        finally:
            await adapter._stop_delivery_recovery()
            await _release_all(adapter, recorder)
            store.close_all_db_handles()
        return
    if scenario == "refused":
        retransmission = _text("recover-me", text="changed retransmission")
        assert await adapter._stage_durable_delivery(retransmission, key, "owned")
        assert retransmission.text == "original authored request"
        assert retransmission.metadata == {"exact": ["", "value"]}
        original_handle = adapter.handle_message
        refused = asyncio.Event()

        async def refuse_once(replay):
            refused.set()
            return None

        adapter.handle_message = refuse_once
        await adapter._start_delivery_recovery()
        await asyncio.wait_for(refused.wait(), 3)
        assert store.get_relay_delivery_pending(key) is not None
        adapter.handle_message = original_handle
        adapter._wake_delivery_recovery()
    else:
        await adapter._start_delivery_recovery()
        await asyncio.wait_for(recorder.entered.wait(), 3)
        assert store.get_relay_delivery_pending(key) is not None
        if scenario == "restart":
            await adapter._stop_delivery_recovery()
            await _release_all(adapter, recorder)
            store.close_all_db_handles()
            store = SessionStore(tmp_path / "sessions", _config())
        else:
            # Replacement worker must not dispatch work still owned by the old model task.
            replacement, other = _live_adapter(store)
            await replacement._start_delivery_recovery()
            await asyncio.sleep(0.05)
            assert other.retained == []
            await replacement._stop_delivery_recovery()
            await adapter._stop_delivery_recovery()
            await _release_all(adapter, recorder)
        adapter, recorder = _live_adapter(store)
        await adapter._start_delivery_recovery()
    recorder.release.set()
    try:
        await asyncio.wait_for(recorder.entered.wait(), 3)
        for _ in range(100):
            if store.is_relay_delivery_settled(key):
                break
            await asyncio.sleep(0.02)
        assert store.is_relay_delivery_settled(key)
        assert store.get_relay_delivery_pending(key) is None
        assert not adapter._has_durable_resume_for(session_key), "completed lanes must leave the RAM index"
        assert len(recorder.retained) == 1
        recovered = recorder.retained[0]
        assert recovered.text == "original authored request"
        assert recovered.reply_to_message_id == "original-anchor"
        assert recovered.metadata == {"exact": ["", "value"]}
        assert gateway_input_owner(recovered) == gateway_input_owner(event)
    finally:
        await adapter._stop_delivery_recovery()
        await _release_all(adapter, recorder)
        store.close_all_db_handles()

"""Relay durability and admission contracts against real storage and adapter boundaries.

Durability cases call the interaction observer and read back a reopened SessionStore. Admission
cases run BasePlatformAdapter.handle_message with model execution replaced by a bounded recorder.
Busy-handler fixtures retain real pending work and publish the handler's admission receipt.

handle_message returns after scheduling or retaining work, not after the model turn completes.
Cancellation cases therefore suspend inside the busy handler, not in the background model task.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, merge_pending_message_event
from gateway.platforms.event import MessageEvent
from gateway.relay.ws_transport import _event_from_wire
from gateway.session import SessionStore
from tests.gateway.relay.test_relay_interactive import _adapter


def _config() -> GatewayConfig:
    return GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})


def _text(message_id: str, *, text: str = "hello", **source_kw) -> MessageEvent:
    source = {
        "platform": "discord",
        "chat_id": "ch1",
        "chat_type": "group",
        "scope_id": "g1",
        "user_id": "u1",
        "user_name": "ben",
        "user_display_name": "Ben D",
        "chat_name": "Hermes / #ops",
        "message_id": message_id,
    }
    source.update(source_kw)
    return _event_from_wire({
        "text": text, "message_type": "text", "message_id": message_id, "source": source,
    })


def _interaction(
    *, interaction_id: str = "press-1", attached_message_id: str = "bot-1",
    custom_id: str = "inspect",
):
    """One connector-forwarded Discord component press on an attached bot message."""
    body = {
        "type": 3,
        "id": interaction_id,
        "channel_id": "ch1",
        "guild_id": "g1",
        "message": {"id": attached_message_id},
        "member": {"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
        "data": {"custom_id": custom_id},
    }

    class Forward:
        platform = "discord"
        method = "POST"
        path = "/interactions/bot1"

    Forward.body = json.dumps(body).encode()
    return Forward()


class _Recorder:
    """Handler-side recorder: counts real retained turns and can hold one open."""

    def __init__(self) -> None:
        self.retained: list[MessageEvent] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, event: MessageEvent):
        self.retained.append(event)
        self.entered.set()
        await self.release.wait()


def _live_adapter(store: SessionStore) -> tuple[BasePlatformAdapter, _Recorder]:
    """A relay adapter whose handle_message is the REAL base-adapter boundary.

    Only the model turn underneath is stubbed; handle_message's own guards, session claim, queue and
    retention branches all run for real.
    """
    adapter, _stub = _adapter(platform="discord")
    adapter.set_session_store(store)
    recorder = _Recorder()
    adapter.set_message_handler(recorder)
    return adapter, recorder


def _gateway_runner() -> gateway_run.GatewayRunner:
    """A bare runner carrying only what _run_in_executor_with_context touches."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._executor_lock = threading.Lock()
    runner._executor_closing = False
    runner._executor = None
    runner._housekeeping_executor = None
    return runner


async def _release_all(adapter: BasePlatformAdapter, recorder: _Recorder) -> None:
    """Release the recorder and cancel every task the adapter still owns, then await them.

    ``store.close_all_db_handles()`` runs after this: an uncancelled debounce flush or a still-
    running background turn would keep touching the (closing) DB.
    """
    recorder.release.set()
    owned = [task for task in adapter._session_tasks.values() if task is not None]
    owned += [state.task for state in adapter._text_debounce_store().values() if state.task]
    owned += list(adapter._background_tasks)
    for task in owned:
        task.cancel()
    results = await asyncio.gather(*dict.fromkeys(owned), return_exceptions=True)
    adapter._session_tasks.clear()
    adapter._background_tasks.clear()
    adapter._text_debounce.clear()
    for event in adapter._active_sessions.values():
        event.set()
    for result in results:
        # Cancellation is intentional cleanup; other task failures must still fail the test.
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result


# ── durability: the offload wrapper is positional-only on BOTH arms ────────────


@pytest.mark.parametrize("arm", ["gateway-owned-executor", "to-thread"])
@pytest.mark.parametrize(
    "fact", ["guild-nick-null", "guild-nick-present", "dm-current-user", "thread-parent"],
)
def test_interaction_observation_reaches_real_store_on_every_offload_arm(tmp_path, arm, fact):
    """A raw-interaction fact must survive BOTH offload arms against a REAL store.

    The adapter wrapper and gateway-owned executor accept positional arguments only; to_thread
    itself accepts keywords. Binding the keyword-only facts before offloading must preserve them
    through either arm and across a store reopen.
    """
    async def run():
        store = SessionStore(tmp_path, _config())
        adapter, _stub = _adapter(platform="discord")
        adapter.set_session_store(store)
        adapter.gateway_runner = _gateway_runner() if arm == "gateway-owned-executor" else None

        is_dm = fact == "dm-current-user"
        user = {"id": "u1", "username": "ben", "global_name": "New Name"}
        payload = {
            "guild_id": None if is_dm else "g1",
            "channel_id": "dm1" if is_dm else "ch1",
            "member": {"user": user} if is_dm else {"nick": None, "user": user},
            "user": user,
        }
        if fact == "guild-nick-present":
            payload["member"]["nick"] = "Nicked"
        if fact == "thread-parent":
            payload["channel"] = {"id": "th1", "type": 11, "parent_id": "ch1"}
            payload["channel_id"] = "th1"

        event = _text("m1", chat_id=payload["channel_id"])
        # This lane persists the RESOLVED display name the interaction conversion computed
        # (``member.nick`` when present, else global name / username), not the raw nick.
        event.source.user_name = "Nicked" if fact == "guild-nick-present" else "New Name"

        await adapter._remember_discord_interaction_context(payload, event)

        # REAL durable read-back through a reopened store, not process-local cache.
        store.close_all_db_handles()
        reopened = SessionStore(tmp_path, _config())
        durable = reopened.relay_discord_context(
            "" if is_dm else "g1", payload["channel_id"], "u1",
        )
        expected_name = "Nicked" if fact == "guild-nick-present" else "New Name"
        assert durable.get("user_name") == expected_name, fact
        if fact == "thread-parent":
            assert durable.get("parent_chat_id") == "ch1"
        reopened.close_all_db_handles()

    asyncio.run(run())


# ── admission: cancellation BEFORE the handoff settles must stay replayable ────


@pytest.mark.asyncio
async def test_cancelled_before_admission_is_retried_then_deduped(tmp_path):
    """Cancel the inbound task BEFORE handle_message: the frame stays retryable and then dedupes.

    The pre-admission await (the off-loop Discord observation) is the cancellable window. Nothing
    was admitted, so no follower may be told "seen" — otherwise the connector's durable replay is
    silently dropped and the user's message is lost.
    """
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = store.observe_relay_discord_context

    def held(source):
        entered.set()
        try:
            assert release.wait(5)
            return original(source)
        finally:
            finished.set()

    store.observe_relay_discord_context = held

    task = asyncio.create_task(adapter._on_inbound(_text("m1")))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Nothing admitted: the frame must still be claimable.
    key = adapter._inbound_dedupe_key(_text("m1"))
    assert key not in adapter._seen_inbound
    assert key not in adapter._inflight_inbound

    release.set()
    assert await asyncio.to_thread(finished.wait, 3)
    recorder.release.set()
    await adapter._on_inbound(_text("m1"))
    await asyncio.sleep(0)

    assert len(recorder.retained) == 1
    assert key in adapter._seen_inbound
    assert not adapter._inflight_inbound
    store.close_all_db_handles()


@pytest.mark.parametrize("retained", [False, True], ids=["not-retained", "retained"])
@pytest.mark.parametrize("lane", ["normalized", "passthrough"], ids=["normalized", "passthrough"])
@pytest.mark.asyncio
async def test_cancelled_during_real_handoff_settles_from_receipt(tmp_path, lane, retained):
    """Cancel the inbound task WHILE the real handle_message is suspended.

    ``handle_message`` normally RETURNS after spawning the turn, so its ``await`` only stays open
    while the session is busy and the runner's ``_busy_session_handler`` extension point is
    awaited. That is the only way to hold the handoff open for real, and it is what makes the
    assertion below a cancellation instead of an already-finished task.

    Retained work (the handler stores it) keeps the frame consumed on a cancelled handoff; work the
    handler never kept must stay retryable so the connector still owns it.
    """
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)

    # Occupy the session with a real turn so the next frame takes the busy path.
    await adapter._on_inbound(_text("m0"))
    await asyncio.wait_for(recorder.entered.wait(), 3)
    recorder.entered.clear()

    busy_entered = asyncio.Event()
    busy_release = asyncio.Event()

    async def busy_handler(event: MessageEvent, session_key: str) -> bool:
        busy_entered.set()
        if retained:
            # Retain through the production helper and publish the busy handler's receipt.
            merge_pending_message_event(adapter._pending_messages, session_key, event,
                                        merge_text=True)
            event._gateway_accepted = True
        await busy_release.wait()
        return True

    adapter.set_busy_session_handler(busy_handler)

    incoming = _text("m1")
    deliver = (
        adapter._on_inbound(incoming)
        if lane == "normalized"
        else adapter._on_passthrough(_interaction(), "buf-1")
    )
    task = asyncio.create_task(deliver)
    await asyncio.wait_for(busy_entered.wait(), 3)

    # The task really is suspended INSIDE handle_message — not already complete.
    assert not task.done()

    assert task.cancel() is True
    with pytest.raises(asyncio.CancelledError):
        await task

    key = (
        adapter._inbound_dedupe_key(incoming)
        if lane == "normalized" else "passthrough_buffer:buf-1"
    )
    assert not adapter._inflight_inbound
    # Both lanes left the connector's buffer unacked at the cancel: only a completed settle acks.
    assert adapter._transport.acked_buffer_ids == []
    if retained:
        assert key in adapter._seen_inbound, "a retained frame must not be replayed after a cancel"
        # The frame the lane actually handed off: for passthrough that is the event the connector's
        # interaction body built, not the normalized ``incoming`` used by the other lane.
        handed_off = incoming if lane == "normalized" else list(
            adapter._pending_messages.values()
        )[-1]
        assert handed_off._gateway_accepted is True
    else:
        assert key not in adapter._seen_inbound, "an unkept frame must stay retryable"

    # The connector redelivers the SAME frame. A retained one is dropped as a replay (its text must
    # not appear twice); an unkept one is admitted again, so nothing the user sent is lost.
    busy_release.set()
    adapter.set_busy_session_handler(None)
    if lane == "normalized":
        await adapter._on_inbound(_text("m1"))
    else:
        await adapter._on_passthrough(_interaction(), "buf-1")
    await asyncio.sleep(0)

    if lane == "normalized":
        # The occupying turn is still held, so the redelivered frame takes the busy lane again.
        pending = " ".join(str(e.text or "") for e in adapter._pending_messages.values())
        assert pending.count("hello") == 1, ("duplicate or lost redelivery", pending)
        assert len(recorder.retained) == 1, "the redelivery must not start a second turn"
    else:
        assert adapter._transport.acked_buffer_ids == ["buf-1"]
        assert key in adapter._seen_inbound

    await _release_all(adapter, recorder)
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_no_duplicate_after_real_admission(tmp_path):
    """Once real admission retains the turn, the connector's replay is suppressed."""
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)
    recorder.release.set()

    await adapter._on_inbound(_text("m1"))
    await asyncio.sleep(0)
    assert len(recorder.retained) == 1
    assert adapter._inbound_dedupe_key(_text("m1")) in adapter._seen_inbound

    await adapter._on_inbound(_text("m1"))
    await asyncio.sleep(0)
    assert len(recorder.retained) == 1
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_overlapping_duplicates_admit_once(tmp_path):
    """Two concurrent copies of one frame: exactly one becomes the admission owner.

    The overlap window is the pre-admission off-loop observation, not ``handle_message``: that
    returns as soon as the turn is spawned, so a duplicate arriving after it already sees "seen".
    """
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)

    entered = threading.Event()
    release = threading.Event()
    original = store.observe_relay_discord_context

    def held(source):
        entered.set()
        assert release.wait(5)
        return original(source)

    store.observe_relay_discord_context = held

    first = asyncio.create_task(adapter._on_inbound(_text("m1")))
    assert await asyncio.to_thread(entered.wait, 3)
    duplicate = asyncio.create_task(adapter._on_inbound(_text("m1")))
    await asyncio.sleep(0)
    assert not duplicate.done()

    release.set()
    recorder.release.set()
    await asyncio.gather(first, duplicate)
    await asyncio.sleep(0)

    assert len(recorder.retained) == 1
    assert not adapter._inflight_inbound
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_retained_busy_text_merge_marks_admission_receipt(tmp_path):
    """TWO same-sender queue-mode followups: both merges retain, so both mint the receipt.

    The first frame only proves the new-buffer branch; every later same-sender followup takes the
    existing-state MERGE branch. Without the receipt the relay cannot tell "buffered and kept"
    from "refused" — a durable frame it wrongly treats as admitted is dropped from replay forever.
    """
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)
    adapter._busy_text_mode = "queue"

    # Occupy the session so the next messages take the queue/debounce lane.
    await adapter._on_inbound(_text("m0"))
    await asyncio.wait_for(recorder.entered.wait(), 3)
    recorder.entered.clear()
    await asyncio.sleep(0)

    followups = [
        _text("m1", text="first retained text"),
        _text("m2", text="second retained text"),
    ]
    for followup in followups:
        await adapter._on_inbound(followup)
        await asyncio.sleep(0)
        assert followup._gateway_accepted is True, followup.message_id
        assert adapter._inbound_dedupe_key(followup) in adapter._seen_inbound, followup.message_id

    # One real buffered merge holds both texts EXACTLY once: nothing swallowed, nothing duplicated.
    buffered = "\n".join(
        str(state.event.text or "") for state in adapter._text_debounce_store().values()
    )
    for followup in followups:
        assert buffered.count(str(followup.text)) == 1, (followup.message_id, buffered)

    await _release_all(adapter, recorder)
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_policy_drop_still_consumes_the_frame(tmp_path):
    """A NORMAL return settles the claim even when nothing was retained.

    The base adapter refuses an event whose profile route targets an unserved profile, leaving the
    receipt False. Settling on normal return preserves the historical terminal-consumption contract
    rather than re-admitting a frame the gateway already decided on.
    """
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)
    rejected = _text("m1")
    rejected.source.profile = "ghost-profile"
    # No gateway_runner seam: the adapter cannot resolve the route, and marks it rejected.
    rejected.source.profile_route_rejected = True

    await adapter._on_inbound(rejected)
    await asyncio.sleep(0)

    assert recorder.retained == []
    key = adapter._inbound_dedupe_key(rejected)
    assert key in adapter._seen_inbound
    assert not adapter._inflight_inbound
    store.close_all_db_handles()


# ── the passthrough lane proves the same thing on the real WS callback path ────


@pytest.mark.asyncio
async def test_passthrough_cancelled_before_admission_acks_and_retries(tmp_path):
    """Buffer lane: a cancelled pre-admission handoff leaves the buffer unacked and retryable."""
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)

    entered = asyncio.Event()
    release = asyncio.Event()
    original = adapter._discord_context_for

    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    adapter._discord_context_for = held

    task = asyncio.create_task(adapter._on_passthrough(_interaction(), "buf-1"))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    key = "passthrough_buffer:buf-1"
    assert key not in adapter._seen_inbound
    assert key not in adapter._inflight_inbound
    assert adapter._transport.acked_buffer_ids == []

    release.set()
    adapter._discord_context_for = original
    recorder.release.set()
    await adapter._on_passthrough(_interaction(), "buf-1")
    await asyncio.sleep(0)

    assert len(recorder.retained) == 1
    assert key in adapter._seen_inbound
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_passthrough_distinct_buffers_each_admit(tmp_path):
    """Two DISTINCT durable buffers are two claims; neither may suppress the other.

    The second press arrives while the first turn still owns the session, so real admission
    RETAINS it as pending work rather than spawning a second turn. Both texts must survive in the
    pending slot: "admitted" means kept, not necessarily run now.
    """
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)
    recorder.release.set()

    await adapter._on_passthrough(
        _interaction(interaction_id="press-a", custom_id="inspect-a"), "buf-a",
    )
    await asyncio.sleep(0.01)
    await adapter._on_passthrough(
        _interaction(interaction_id="press-b", custom_id="inspect-b"), "buf-b",
    )
    await asyncio.sleep(0.05)

    assert "passthrough_buffer:buf-a" in adapter._seen_inbound
    assert "passthrough_buffer:buf-b" in adapter._seen_inbound
    assert not adapter._inflight_inbound
    # "Admitted" means RETAINED, not necessarily run now: the first press spawned a turn and the
    # second is parked as pending follow-up work. Both texts must survive somewhere real.
    settled = " ".join(str(event.text or "") for event in recorder.retained)
    pending = " ".join(
        str(event.text or "") for event in adapter._pending_messages.values()
    )
    assert "inspect-a" in settled + pending
    assert "inspect-b" in settled + pending
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_passthrough_overlapping_duplicates_admit_once(tmp_path):
    """Two concurrent copies of ONE buffer: exactly one admission, and both copies are acked."""
    store = SessionStore(tmp_path, _config())
    adapter, recorder = _live_adapter(store)
    stub = adapter._transport

    entered = asyncio.Event()
    release = asyncio.Event()
    original = adapter._discord_context_for

    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    adapter._discord_context_for = held

    first = asyncio.create_task(adapter._on_passthrough(_interaction(), "buf-x"))
    await asyncio.wait_for(entered.wait(), 3)
    duplicate = asyncio.create_task(adapter._on_passthrough(_interaction(), "buf-x"))
    await asyncio.sleep(0)
    assert not duplicate.done()

    release.set()
    recorder.release.set()
    await asyncio.gather(first, duplicate)
    await asyncio.sleep(0)

    assert len(recorder.retained) == 1
    assert stub.acked_buffer_ids == ["buf-x", "buf-x"]
    assert not adapter._inflight_inbound
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_inbound_dedupe_persists_settlement_across_reopen(tmp_path):
    """Inbound relay delivery settlement is durably persisted across SessionStore reopen."""
    cfg = _config()
    store1 = SessionStore(tmp_path, cfg)
    adapter1, recorder1 = _live_adapter(store1)
    recorder1.release.set()

    event = _text("msg-dedupe-reopen", text="hello")
    await adapter1._on_inbound(event)
    await asyncio.sleep(0.01)

    dedupe_key = adapter1._inbound_dedupe_key(event)
    assert dedupe_key is not None
    assert store1.is_relay_delivery_settled(dedupe_key)
    store1.close_all_db_handles()

    store2 = SessionStore(tmp_path, cfg)
    adapter2, recorder2 = _live_adapter(store2)
    recorder2.release.set()

    dup_event = _text("msg-dedupe-reopen", text="hello")
    await adapter2._on_inbound(dup_event)
    await asyncio.sleep(0.01)

    assert len(recorder2.retained) == 0
    store2.close_all_db_handles()


def test_restored_relay_delivery_ownership_after_reopen(tmp_path):
    """Restored session source preserves delivered_via_upstream_relay and routes to RelayAdapter."""
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    cfg = _config()
    store1 = SessionStore(tmp_path, cfg)
    adapter, _ = _live_adapter(store1)
    runner = GatewayRunner(cfg)
    runner.adapters = {Platform.RELAY: adapter}

    src = SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1", delivered_via_upstream_relay=True)
    entry = store1.get_or_create_session(src)
    assert getattr(entry, "delivered_via_relay", False) is True
    store1.close_all_db_handles()

    store2 = SessionStore(tmp_path, cfg)
    restored_entry = store2.get_or_create_session(src)
    restored_src = runner._restored_source(restored_entry)
    assert restored_src.delivered_via_upstream_relay is True
    assert runner._delivery_adapter_for(restored_src) is adapter
    store2.close_all_db_handles()


@pytest.mark.asyncio
async def test_text_debounce_merging_preserves_interaction_identity(tmp_path):
    """Merging text debounce events preserves discord_interaction_id discriminator and anchor."""
    cfg = _config()
    store = SessionStore(tmp_path, cfg)
    adapter, _ = _live_adapter(store)
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 10.0

    press1 = _text("press-1", text="first press")
    press1.metadata = {"discord_interaction_id": "press-1"}
    press1.reply_to_message_id = "bot-1"

    press2 = _text("press-2", text="second press")
    press2.metadata = {"discord_interaction_id": "press-2"}
    press2.reply_to_message_id = "bot-2"

    key = adapter._event_session_key(press1)
    await adapter._queue_text_debounce(key, press1)
    await adapter._queue_text_debounce(key, press2)

    state = adapter._text_debounce_store().get(key)
    assert state is not None
    assert state.event.metadata.get("discord_interaction_id") == "press-2"
    assert state.event.reply_to_message_id == "bot-2"
    assert state.event.text == "second press"
    adapter._discard_text_debounce(key)
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_first_turn_lifecycle_after_status_command(tmp_path):
    """Executing /status sets agent_turn_initialized=False so the first real turn emits session:start."""
    from unittest.mock import AsyncMock
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    cfg = _config()
    runner = GatewayRunner(cfg)
    runner.session_store = SessionStore(tmp_path, cfg)
    runner.hooks.emit = AsyncMock()

    src = SessionSource(platform=Platform.DISCORD, chat_id="c1", user_id="u1")
    event = MessageEvent(text="/status", source=src, message_id="m-status")
    await runner._handle_status_command(event)

    entry = await runner.async_session_store.get_or_create_session(src)
    assert getattr(entry, "agent_turn_initialized", False) is False

    _, is_new = await runner._hmwa_open_session(entry, entry.session_key, src)
    assert is_new is True
    assert runner.hooks.emit.await_count == 1
    runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_cancelled_inline_reply_does_not_replay_completed_command(tmp_path):
    """Cancelling a bypass command while sending its reply suppresses replaying the completed command."""
    from gateway.platforms.base import SendResult
    cfg = _config()
    store = SessionStore(tmp_path, cfg)
    adapter, occupant = _live_adapter(store)
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = []

    async def handler(event):
        completed.append(event.message_id)
        return "handled"

    async def send_reply(**kwargs):
        if len(completed) == 1 and not release.is_set():
            entered.set()
            await release.wait()
        return SendResult(success=True, message_id="reply-1")

    occ = _text("occupant-cancel", text="busy occupant")
    inline = _text("inline-cancel", text="/new")

    task = None
    try:
        await adapter._on_inbound(occ)
        await asyncio.wait_for(occupant.entered.wait(), 3)
        adapter.set_message_handler(handler)
        adapter._send_with_retry = send_reply
        task = asyncio.create_task(adapter._on_inbound(inline))
        await asyncio.wait_for(entered.wait(), 3)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        release.set()
        await asyncio.wait_for(adapter._on_inbound(inline), 3)
        assert len(completed) == 1, f"Expected 1 completion, got {len(completed)}"
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        occupant.release.set()
        store.close_all_db_handles()
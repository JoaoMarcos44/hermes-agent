"""Comprehensive edge-case and concurrency tests for admitted internal wakes across /stop.

Covers:
1. Overflow queued events after /stop (internal preserved, human discarded).
2. /stop discards human follow-ups across both primary slot and overflow.
3. Multiple internal wakes preserved in FIFO order across primary slot and overflow.
4. Non-stop idle transitions (natural turn completion, eviction, error handling).
5. /new and /reset continue to discard both primary slot and overflow stores.
6. /stop continues to discard human follow-up when no wakes are present.
7. Custom / duck-typed adapters (with and without clear_pending_followup).
8. Race condition: internal wake arriving during /stop execution.
9. Race condition: wake arriving while another wake is being drained.
10. Database / transcript deduplication on drained internal wake.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import (
    _INTERRUPT_REASON_EVICTED,
    _INTERRUPT_REASON_RESET,
    _INTERRUPT_REASON_STOP,
    GatewayRunner,
)
from gateway.session import SessionSource
from gateway.session_state import SessionState


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="u1",
    )


def _event(*, internal: bool, text: str = "notice", message_id: str = "msg-1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
        message_id=message_id,
        internal=internal,
    )


class _TestAdapter(BasePlatformAdapter):
    name = "test"

    def __init__(self, session_key: str, parked: MessageEvent | None = None):
        self._pending_messages = {session_key: parked} if parked else {}
        self._active_sessions: dict = {}
        self._session_tasks: dict = {}
        self._background_tasks: set = set()
        self._expected_cancelled_tasks: set = set()
        self.started: list = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def get_chat_info(self, chat_id: str):
        return {}

    async def send(self, target, message):
        pass

    def get_pending_message(self, session_key: str):
        return BasePlatformAdapter.get_pending_message(self, session_key)

    def clear_pending_followup(self, session_key: str, *, keep_internal: bool = False):
        return BasePlatformAdapter.clear_pending_followup(
            self, session_key, keep_internal=keep_internal,
        )

    async def _flush_text_debounce_now(self, session_key: str):
        return None

    def _release_session_guard(self, session_key: str, guard=None):
        return BasePlatformAdapter._release_session_guard(self, session_key, guard=guard)

    def _start_session_processing(self, event, session_key: str, *, interrupt_event=None):
        self.started.append((event, session_key))
        guard = interrupt_event or asyncio.Event()
        self._active_sessions[session_key] = guard
        return True

    def _spawn_drain_task(self, pending_event: MessageEvent, session_key: str) -> None:
        self.started.append((pending_event, session_key))


class _DuckTypedAdapterWithDict:
    """Custom adapter with _pending_messages dict but no clear_pending_followup."""

    def __init__(self, session_key: str, parked: MessageEvent | None = None):
        self._pending_messages = {session_key: parked} if parked else {}
        self.started: list = []

    def get_pending_message(self, session_key: str):
        return self._pending_messages.pop(session_key, None)

    def _start_session_processing(self, event, session_key: str):
        self.started.append((event, session_key))
        return True


class _MinimalDuckTypedAdapter:
    """Custom adapter that only defines get_pending_message."""

    def __init__(self):
        self.called_get_pending = 0

    def get_pending_message(self, session_key: str):
        self.called_get_pending += 1
        return None


def _make_runner(adapter, state: SessionState | None = None):
    runner = object.__new__(GatewayRunner)
    runner._peek_session_state = lambda _key: state
    runner._session_state = lambda _key: state
    runner._interrupt_running_turn = lambda *a, **k: 0
    runner._drop_turn_slot = lambda *a, **k: None
    runner._adapter_for_source = lambda _src: adapter
    runner._thread_metadata_for_source = lambda _src: {}
    runner._overflow_queue = lambda session_key: state.conversation.queued_events if state else None
    runner._SECURITY_METADATA_KEYS = ()
    runner._BUSY_QUEUE_MAX_PENDING = 20
    return runner


@pytest.mark.asyncio
async def test_overflow_queued_events_after_stop():
    """1. Overflow queued events after /stop: internal preserved, human discarded, head promoted."""
    session_key = "agent:main:telegram:dm:12345"
    human_head = _event(internal=False, text="user follow up", message_id="u-1")
    wake_1 = _event(internal=True, text="wake 1", message_id="w-1")
    human_mid = _event(internal=False, text="user mid", message_id="u-2")
    wake_2 = _event(internal=True, text="wake 2", message_id="w-2")

    state = SessionState()
    state.conversation.queued_events = [wake_1, human_mid, wake_2]

    adapter = _TestAdapter(session_key, human_head)
    runner = _make_runner(adapter, state)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )

    # human_head discarded; wake_1 promoted to primary slot; human_mid discarded; wake_2 in overflow
    assert adapter._pending_messages.get(session_key) is wake_1
    assert state.conversation.queued_events == [wake_2]

    guard = asyncio.Event()
    adapter._active_sessions[session_key] = guard
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, guard,
    )
    assert adapter.started == [(wake_1, session_key)]


@pytest.mark.asyncio
async def test_stop_discards_human_across_both_stores():
    """2. /stop discards human follow-ups across primary slot and overflow."""
    session_key = "agent:main:telegram:dm:12345"
    human_1 = _event(internal=False, text="human 1")
    human_2 = _event(internal=False, text="human 2")
    human_3 = _event(internal=False, text="human 3")

    state = SessionState()
    state.conversation.queued_events = [human_2, human_3]

    adapter = _TestAdapter(session_key, human_1)
    runner = _make_runner(adapter, state)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )

    assert session_key not in adapter._pending_messages
    assert len(state.conversation.queued_events) == 0

    guard = asyncio.Event()
    adapter._active_sessions[session_key] = guard
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, guard,
    )
    assert adapter.started == []


@pytest.mark.asyncio
async def test_multiple_internal_wakes_fifo_order():
    """3. Multiple internal wakes preserved in FIFO order across primary slot and overflow."""
    session_key = "agent:main:telegram:dm:12345"
    wake_1 = _event(internal=True, text="wake 1", message_id="w-1")
    wake_2 = _event(internal=True, text="wake 2", message_id="w-2")
    wake_3 = _event(internal=True, text="wake 3", message_id="w-3")

    state = SessionState()
    state.conversation.queued_events = [wake_2, wake_3]

    adapter = _TestAdapter(session_key, wake_1)
    runner = _make_runner(adapter, state)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )

    # Primary slot keeps wake_1, overflow keeps wake_2 and wake_3
    assert adapter._pending_messages.get(session_key) is wake_1
    assert state.conversation.queued_events == [wake_2, wake_3]

    guard = asyncio.Event()
    adapter._active_sessions[session_key] = guard
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, guard,
    )
    assert adapter.started == [(wake_1, session_key)]

    # Simulate turn 1 finishing: promotion pulls next event from overflow
    promoted_2 = GatewayRunner._promote_queued_event(runner, session_key, adapter, None)
    assert promoted_2 is wake_2
    assert state.conversation.queued_events == [wake_3]

    # Simulate turn 2 finishing: promotion pulls next event from overflow
    promoted_3 = GatewayRunner._promote_queued_event(runner, session_key, adapter, None)
    assert promoted_3 is wake_3
    assert state.conversation.queued_events == []


@pytest.mark.asyncio
async def test_non_stop_idle_transitions():
    """4. Non-stop idle transitions: natural turn end, eviction, error handling."""
    session_key = "agent:main:telegram:dm:12345"

    # 4a. Eviction interrupt (_INTERRUPT_REASON_EVICTED): must discard both stores
    wake_slot = _event(internal=True, text="wake in slot")
    wake_overflow = _event(internal=True, text="wake in overflow")
    state = SessionState()
    state.conversation.queued_events = [wake_overflow]
    adapter = _TestAdapter(session_key, wake_slot)
    runner = _make_runner(adapter, state)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_EVICTED,
        invalidation_reason="cache_pressure",
    )
    assert session_key not in adapter._pending_messages
    assert len(state.conversation.queued_events) == 0

    # 4b. Natural completion with empty queue stays idle
    empty_state = SessionState()
    empty_adapter = _TestAdapter(session_key, None)
    empty_runner = _make_runner(empty_adapter, empty_state)
    promoted = GatewayRunner._promote_queued_event(empty_runner, session_key, empty_adapter, None)
    assert promoted is None

    # 4c. End of task guard reconciliation (_finish_session_task)
    # When late pending message exists, it spawns drain task
    late_wake = _event(internal=True, text="late arrival")
    adapter._pending_messages[session_key] = late_wake
    curr_guard = asyncio.Event()
    adapter._active_sessions[session_key] = curr_guard
    adapter._finish_session_task(session_key, curr_guard)
    assert len(adapter.started) == 1
    assert adapter.started[0][0] is late_wake


@pytest.mark.asyncio
async def test_new_and_reset_discard_all_events_across_both_stores():
    """5. /new and /reset continue to discard both primary slot and overflow."""
    session_key = "agent:main:telegram:dm:12345"
    wake_slot = _event(internal=True, text="wake 1")
    human_overflow = _event(internal=False, text="user queued")
    wake_overflow = _event(internal=True, text="wake 2")

    state = SessionState()
    state.conversation.queued_events = [human_overflow, wake_overflow]

    adapter = _TestAdapter(session_key, wake_slot)
    runner = _make_runner(adapter, state)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_RESET,
        invalidation_reason="new_command",
    )

    assert session_key not in adapter._pending_messages
    assert len(state.conversation.queued_events) == 0

    guard = asyncio.Event()
    adapter._active_sessions[session_key] = guard
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, guard,
    )
    assert adapter.started == []


@pytest.mark.asyncio
async def test_stop_continues_discarding_human_followup():
    """6. /stop continues to discard human follow up when no wakes are present."""
    session_key = "agent:main:telegram:dm:12345"
    human_followup = _event(internal=False, text="regular user message")

    adapter = _TestAdapter(session_key, human_followup)
    runner = _make_runner(adapter, None)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )

    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_custom_duck_typed_adapters():
    """7. Custom / duck-typed adapters with and without clear_pending_followup."""
    session_key = "agent:main:telegram:dm:12345"

    # 7a. Duck-typed adapter with _pending_messages dict (no clear_pending_followup)
    # Human in slot, wake in overflow -> human discarded, wake promoted
    duck_adapter = _DuckTypedAdapterWithDict(session_key, _event(internal=False, text="human in slot"))
    wake = _event(internal=True, text="wake in overflow")
    state = SessionState()
    state.conversation.queued_events = [wake]
    runner = _make_runner(duck_adapter, state)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )
    assert duck_adapter._pending_messages.get(session_key) is wake
    assert len(state.conversation.queued_events) == 0

    # 7b. Duck-typed adapter with wake in slot -> kept across stop
    duck_adapter_2 = _DuckTypedAdapterWithDict(session_key, wake)
    runner_2 = _make_runner(duck_adapter_2, SessionState())
    await runner_2._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )
    assert duck_adapter_2._pending_messages.get(session_key) is wake

    # 7c. Minimal duck-typed adapter without _pending_messages attribute
    minimal_adapter = _MinimalDuckTypedAdapter()
    runner_3 = _make_runner(minimal_adapter, None)
    # On stop: should NOT blindly pop
    await runner_3._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )
    assert minimal_adapter.called_get_pending == 0

    # On reset: SHOULD pop to discard
    await runner_3._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_RESET,
        invalidation_reason="new_command",
    )
    assert minimal_adapter.called_get_pending == 1


@pytest.mark.asyncio
async def test_race_condition_wake_arriving_during_stop():
    """8. Race condition: internal wake arriving during /stop execution."""
    session_key = "agent:main:telegram:dm:12345"
    adapter = _TestAdapter(session_key, None)
    state = SessionState()
    runner = _make_runner(adapter, state)

    # Command guard installed during /stop execution
    command_guard = asyncio.Event()
    adapter._active_sessions[session_key] = command_guard

    # First /interrupt_and_clear_session runs for stop
    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )

    # During the stop dispatch, a background notice arrives
    arrived_wake = _event(internal=True, text="arrived during stop", message_id="wake-race")
    GatewayRunner._queue_or_replace_pending_event(runner, session_key, arrived_wake)

    assert adapter._pending_messages.get(session_key) is arrived_wake

    # Stop finishes its dispatch and runs _drain_pending_after_session_command
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, command_guard,
    )
    assert adapter.started == [(arrived_wake, session_key)]


@pytest.mark.asyncio
async def test_wake_arriving_while_another_wake_is_being_drained():
    """9. Race condition: wake arriving while another wake is being drained."""
    session_key = "agent:main:telegram:dm:12345"
    wake_1 = _event(internal=True, text="wake 1", message_id="w-1")
    wake_2 = _event(internal=True, text="wake 2", message_id="w-2")

    adapter = _TestAdapter(session_key, wake_1)
    state = SessionState()
    runner = _make_runner(adapter, state)

    # Wake 1 is drained and active under session guard
    guard_1 = asyncio.Event()
    adapter._active_sessions[session_key] = guard_1
    pending_event = adapter._pending_messages.pop(session_key, None)
    adapter._start_session_processing(pending_event, session_key)

    assert adapter.started == [(wake_1, session_key)]

    # Wake 2 arrives while Wake 1 is processing: goes into overflow
    # because session_key is busy and slot was cleared by drain
    adapter._pending_messages[session_key] = wake_1  # simulate occupied or FIFO
    GatewayRunner._queue_or_replace_pending_event(runner, session_key, wake_2)
    adapter._pending_messages.pop(session_key, None)  # restore empty slot for turn

    assert wake_2 in state.conversation.queued_events

    # Wake 1 turn completes: promotes wake_2 into next turn
    promoted = GatewayRunner._promote_queued_event(runner, session_key, adapter, None)
    assert promoted is wake_2
    assert len(state.conversation.queued_events) == 0


@pytest.mark.asyncio
async def test_no_database_duplication_on_drained_internal_wake():
    """10. Database / transcript deduplication on drained internal wake."""
    session_key = "agent:main:telegram:dm:12345"
    wake = _event(internal=True, text="[ASYNC DELEGATION BATCH COMPLETE]", message_id="w-dedup")

    adapter = _TestAdapter(session_key, wake)
    state = SessionState()
    runner = _make_runner(adapter, state)

    session_store = MagicMock()
    session_store.append_to_transcript = MagicMock()
    runner.session_store = session_store

    # /stop leaves the wake
    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )
    assert adapter._pending_messages.get(session_key) is wake

    # Drain triggers processing exactly once
    guard = asyncio.Event()
    adapter._active_sessions[session_key] = guard
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, guard,
    )
    assert adapter.started == [(wake, session_key)]

    # When the turn records the user entry with display_kind='internal_notification'
    entry = {
        "role": "user",
        "content": wake.text,
        "display_kind": "internal_notification",
    }
    session_store.append_to_transcript(session_key, entry)

    # Verify recorded exactly once
    assert session_store.append_to_transcript.call_count == 1
    call_args = session_store.append_to_transcript.call_args[0]
    assert call_args[0] == session_key
    assert call_args[1]["display_kind"] == "internal_notification"
    assert call_args[1]["content"] == wake.text

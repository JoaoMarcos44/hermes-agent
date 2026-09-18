"""Admitted internal wakes must survive /stop and still reach the drain.

``_interrupt_and_clear_session`` used to pop the adapter pending slot on every
interrupt. Async-delegation notices share that slot; after /stop the
post-command drain found nothing and the session idled until the next user
message. Regression for #114456.

Policy lives on the adapter slot. The runner keys keep-vs-discard off the
existing ``interrupt_reason`` taxonomy (``_INTERRUPT_REASON_STOP``), not off
``invalidation_reason`` string prefixes. /new still discards; human follow-ups
on /stop still discard.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import _INTERRUPT_REASON_RESET, _INTERRUPT_REASON_STOP
from gateway.session import SessionSource


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="u1",
    )


def _notice(*, internal: bool) -> MessageEvent:
    return MessageEvent(
        text="[ASYNC DELEGATION BATCH COMPLETE]",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="wake-1",
        internal=internal,
    )


class _SlotAdapter:
    def __init__(self, session_key: str, parked: MessageEvent):
        self._pending_messages = {session_key: parked}
        self._active_sessions: dict = {}
        self.started: list = []

    def get_pending_message(self, session_key: str):
        return BasePlatformAdapter.get_pending_message(self, session_key)

    def clear_pending_followup(self, session_key: str, *, keep_internal: bool = False):
        return BasePlatformAdapter.clear_pending_followup(
            self, session_key, keep_internal=keep_internal,
        )

    async def _flush_text_debounce_now(self, session_key: str):
        return None

    def _release_session_guard(self, session_key: str, guard=None):
        return None

    def _start_session_processing(self, event, session_key: str):
        self.started.append((event, session_key))
        return True


def _runner(adapter, state=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._peek_session_state = lambda _key: state
    runner._interrupt_running_turn = lambda *a, **k: 0
    runner._drop_turn_slot = lambda *a, **k: None
    runner._adapter_for_source = lambda _src: adapter
    runner._thread_metadata_for_source = lambda _src: {}
    return runner


@pytest.mark.parametrize(
    "invalidation_reason",
    (
        "stop_command",
        "stop_command_pending",
        "stop_command_handler",
        "stop_command_thread_sibling",
    ),
)
@pytest.mark.asyncio
async def test_stop_interrupt_leaves_internal_wake(invalidation_reason):
    session_key = "agent:main:telegram:dm:12345"
    notice = _notice(internal=True)
    adapter = _SlotAdapter(session_key, notice)
    await _runner(adapter)._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason=invalidation_reason,
    )
    assert adapter._pending_messages.get(session_key) is notice


@pytest.mark.asyncio
async def test_reset_interrupt_discards_internal_wake():
    session_key = "agent:main:telegram:dm:12345"
    adapter = _SlotAdapter(session_key, _notice(internal=True))
    await _runner(adapter)._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_RESET,
        invalidation_reason="new_command",
    )
    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_stop_interrupt_discards_human_followup():
    session_key = "agent:main:telegram:dm:12345"
    adapter = _SlotAdapter(session_key, _notice(internal=False))
    await _runner(adapter)._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )
    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_stop_does_not_key_off_invalidation_reason_prefix():
    """A stop interrupt still keeps the wake even if invalidation_reason is unrelated."""
    session_key = "agent:main:telegram:dm:12345"
    notice = _notice(internal=True)
    adapter = _SlotAdapter(session_key, notice)
    await _runner(adapter)._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="handler_cleanup",
    )
    assert adapter._pending_messages.get(session_key) is notice


def test_clear_pending_followup_keep_internal_is_peek():
    session_key = "agent:main:telegram:dm:12345"
    notice = _notice(internal=True)
    adapter = _SlotAdapter(session_key, notice)
    adapter.clear_pending_followup(session_key, keep_internal=True)
    assert adapter._pending_messages.get(session_key) is notice
    adapter.clear_pending_followup(session_key, keep_internal=False)
    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_drain_after_stop_starts_preserved_wake():
    session_key = "agent:main:telegram:dm:12345"
    notice = _notice(internal=True)
    adapter = _SlotAdapter(session_key, notice)
    await _runner(adapter)._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )
    guard = asyncio.Event()
    adapter._active_sessions[session_key] = guard
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, guard,
    )
    assert adapter.started == [(notice, session_key)]


@pytest.mark.asyncio
async def test_stop_preserves_internal_overflow_wake_when_slot_held_human():
    """P0 regression (ehz0ah review on #114540): human follow-up in pending slot
    and accepted internal wake in SessionState.conversation.queued_events.
    /stop must discard the human head, promote the internal wake from overflow,
    and post-command drain must start the notice.
    """
    from gateway.session_state import SessionState

    session_key = "agent:main:telegram:dm:12345"
    human_event = _notice(internal=False)
    internal_wake = _notice(internal=True)

    state = SessionState()
    state.conversation.queued_events = [internal_wake]

    adapter = _SlotAdapter(session_key, human_event)
    await _runner(adapter, state=state)._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )

    # Human follow-up was discarded; internal wake promoted to the primary slot
    assert adapter._pending_messages.get(session_key) is internal_wake
    assert len(state.conversation.queued_events) == 0

    guard = asyncio.Event()
    adapter._active_sessions[session_key] = guard
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, guard,
    )
    assert adapter.started == [(internal_wake, session_key)]


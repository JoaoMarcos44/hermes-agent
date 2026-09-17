"""Internal wakes must survive every /stop invalidation reason.

``_interrupt_and_clear_session`` used to consume-and-discard the adapter
pending slot for every interrupt. Admitted async-delegation notices share
that slot; after /stop the post-command drain found nothing and the idle
session stalled until the next user message. Regression for #114456.

The helper peeks (does not pop) and skips discard only for stop_command*
reasons when the parked event is internal. /new still discards; human
follow-ups on /stop still discard.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


_STOP_REASONS = (
    "stop_command",
    "stop_command_pending",
    "stop_command_handler",
    "stop_command_thread_sibling",
    "stop_command_no_agent",
)


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="u1",
    )


def _event(*, internal: bool) -> MessageEvent:
    return MessageEvent(
        text="[ASYNC DELEGATION BATCH COMPLETE]",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="msg-wake",
        internal=internal,
    )


def _adapter(session_key: str, parked: MessageEvent):
    adapter = SimpleNamespace(
        _pending_messages={session_key: parked},
        _active_sessions={},
    )
    adapter.get_pending_message = BasePlatformAdapter.get_pending_message.__get__(adapter)
    adapter._flush_text_debounce_now = BasePlatformAdapter._flush_text_debounce_now.__get__(adapter)
    adapter._release_session_guard = BasePlatformAdapter._release_session_guard.__get__(adapter)
    adapter._text_debounce_store = lambda: {}
    return adapter


def _runner(adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._peek_session_state = lambda _key: None
    runner._interrupt_running_turn = lambda *a, **k: None
    runner._drop_turn_slot = lambda *a, **k: None
    runner._adapter_for_source = lambda _src: adapter
    runner._thread_metadata_for_source = lambda _src: {}
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", _STOP_REASONS)
async def test_stop_reasons_leave_parked_internal_wake(reason):
    session_key = "agent:main:telegram:dm:12345"
    notice = _event(internal=True)
    adapter = _adapter(session_key, notice)
    runner = _runner(adapter)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason="user_stop",
        invalidation_reason=reason,
    )

    assert adapter._pending_messages.get(session_key) is notice


@pytest.mark.asyncio
async def test_new_command_still_discards_internal_wake():
    session_key = "agent:main:telegram:dm:12345"
    adapter = _adapter(session_key, _event(internal=True))
    runner = _runner(adapter)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason="user_reset",
        invalidation_reason="new_command",
    )

    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_stop_still_discards_human_followup():
    session_key = "agent:main:telegram:dm:12345"
    adapter = _adapter(session_key, _event(internal=False))
    runner = _runner(adapter)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason="user_stop",
        invalidation_reason="stop_command",
    )

    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_preserved_wake_reaches_session_processing():
    """After /stop, the adapter drain must still hand the notice to a new turn."""
    session_key = "agent:main:telegram:dm:12345"
    notice = _event(internal=True)
    adapter = _adapter(session_key, notice)
    runner = _runner(adapter)

    await runner._interrupt_and_clear_session(
        session_key,
        _source(),
        interrupt_reason="user_stop",
        invalidation_reason="stop_command",
    )

    started = MagicMock(return_value=True)
    adapter._start_session_processing = started
    guard = asyncio.Event()
    adapter._active_sessions[session_key] = guard
    await BasePlatformAdapter._drain_pending_after_session_command(
        adapter, session_key, guard,
    )
    started.assert_called_once_with(notice, session_key)

"""Discrete component actions retain their action/message pair or remain retryable."""

from __future__ import annotations

import asyncio

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.relay.test_relay_interactive import _adapter


def _event(action: str, *, interaction: bool = True, photo: bool = False) -> MessageEvent:
    anchor = f"message-{action}"
    return MessageEvent(
        text=f"action {action}",
        message_type=MessageType.PHOTO if photo else MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="channel", chat_type="group",
            user_id="sender", scope_id="guild", message_id=anchor,
        ),
        message_id=anchor,
        reply_to_message_id=anchor,
        metadata={"discord_interaction_id": action} if interaction else {},
        media_urls=[f"photo-{action}"] if photo else [],
        media_types=["image/png"] if photo else [],
        allow_gateway_control=False,
    )


def _envelope(event: MessageEvent) -> tuple:
    return (
        event.text, event.message_id, event.reply_to_message_id,
        event.source.message_id, event.metadata.get("discord_interaction_id"),
        tuple(event.media_urls),
    )


async def _idle(adapter) -> None:
    async def drain():
        while adapter._background_tasks:
            await asyncio.gather(*tuple(adapter._background_tasks))

    await asyncio.wait_for(drain(), 3)


@pytest.mark.parametrize("mode", ["queue", "interrupt"])
@pytest.mark.asyncio
async def test_busy_component_actions_are_processed_once_or_refused_for_retry(mode):
    """Three presses must preserve all discrete envelopes through the real base drain."""
    adapter, _stub = _adapter(platform="discord")
    adapter._busy_text_mode = mode
    # Completion flushes the buffer; no wall-clock race with the debounce timer is needed.
    adapter._busy_text_debounce_seconds = 30
    adapter._busy_text_hard_cap_seconds = 30
    occupied = _event("occupying", interaction=False)
    entered, release = asyncio.Event(), asyncio.Event()
    received = []

    async def record(event):
        if event is occupied:
            entered.set()
            await release.wait()
        else:
            received.append(_envelope(event))

    adapter.set_message_handler(record)
    incoming = [_event(f"press-{index}") for index in range(3)]
    expected = [_envelope(event) for event in incoming]
    try:
        await adapter.handle_message(occupied)
        await asyncio.wait_for(entered.wait(), 3)
        for event in incoming:
            await adapter.handle_message(event)
        accepted = [event._gateway_accepted for event in incoming]
        assert accepted[0], "an empty pending slot must retain the first component action"
        release.set()
        await _idle(adapter)
        assert received == [value for value, kept in zip(expected, accepted) if kept]

        # The caller retains custody of refused actions and replays them after the drain.
        for event, kept in zip(incoming, accepted):
            if not kept:
                await adapter.handle_message(event)
                assert event._gateway_accepted
                await _idle(adapter)
        assert received == expected
    finally:
        release.set()
        await adapter.cancel_background_tasks()


@pytest.mark.parametrize("mode", ["queue", "interrupt"])
@pytest.mark.parametrize("photo", [False, True], ids=["text", "photo"])
@pytest.mark.parametrize("interaction_first", [False, True])
@pytest.mark.asyncio
async def test_pending_component_never_absorbs_an_ordinary_message(mode, photo, interaction_first):
    """A component and ordinary text/media keep independent action and reply anchors."""
    adapter, _stub = _adapter(platform="discord")
    adapter._busy_text_mode = mode
    adapter._busy_text_debounce_seconds = adapter._busy_text_hard_cap_seconds = 30
    occupied = _event("occupying", interaction=False)
    entered, release = asyncio.Event(), asyncio.Event()
    received = []

    async def record(event):
        if event is occupied:
            entered.set()
            await release.wait()
        else:
            received.append(_envelope(event))

    adapter.set_message_handler(record)
    incoming = [_event("press"), _event("ordinary", interaction=False, photo=photo)]
    if not interaction_first:
        incoming.reverse()
    expected = [_envelope(event) for event in incoming]
    try:
        await adapter.handle_message(occupied)
        await asyncio.wait_for(entered.wait(), 3)
        for event in incoming:
            await adapter.handle_message(event)
        accepted = [event._gateway_accepted for event in incoming]
        release.set()
        await _idle(adapter)
        assert received == [value for value, kept in zip(expected, accepted) if kept]
        for event, kept in zip(incoming, accepted):
            if not kept:
                await adapter.handle_message(event)
                assert event._gateway_accepted
                await _idle(adapter)
        assert received == expected
    finally:
        release.set()
        await adapter.cancel_background_tasks()

"""Tests for /restart idempotency across message and native-interaction redelivery.

Telegram has ordered update ids. Native Slack/Discord interactions instead carry a
platform delivery identity; both must survive a gateway restart strongly enough to
recognize the exact request again without suppressing a genuinely new /restart.
"""
import hashlib
import json
import time
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.platforms.event import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _make_restart_event(update_id: int | None = 100) -> MessageEvent:
    return MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
        platform_update_id=update_id,
    )


@pytest.mark.asyncio
async def test_redelivered_restart_with_older_update_id_is_ignored(tmp_path, monkeypatch):
    """update_id strictly LESS than the recorded one is also a redelivery."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 12345,
        "requested_at": time.time() - 5,
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock()

    event = _make_restart_event(update_id=12344)  # older update — shouldn't happen,
                                                  # but if Telegram does re-deliver
                                                  # something older, treat as stale
    result = await runner._handle_restart_command(event)

    assert result == ""
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_stale_marker_older_than_5min_does_not_block(tmp_path, monkeypatch):
    """A marker older than the 5-minute window is ignored — fresh /restart proceeds."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 12345,
        "requested_at": time.time() - 600,  # 10 minutes ago
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    # Same update_id as the stale marker, but the marker is too old to trust
    event = _make_restart_event(update_id=12345)
    await runner._handle_restart_command(event)

    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_event_without_restart_identity_bypasses_dedup(tmp_path, monkeypatch):
    """Events with neither update id nor native delivery id are not gated."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 999999,
        "requested_at": time.time(),
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    # No update id or platform delivery id — the dedup check should NOT kick in.
    event = _make_restart_event(update_id=None)
    await runner._handle_restart_command(event)

    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_native_delivery_redelivery_is_ignored_by_fingerprint(tmp_path, monkeypatch):
    """Native slash/interaction retries have identity even when there is no message id."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    raw_id = "slack-trigger-capability"
    fingerprint = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
    (tmp_path / ".restart_last_processed.json").write_text(json.dumps({
        "platform": "slack",
        "delivery_id_hash": fingerprint,
        "requested_at": time.time(),
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.SLACK, chat_id="C1", chat_type="group", user_id="U1"),
        platform_delivery_id=raw_id,
    )

    result = await runner._handle_restart_command(event)

    assert result == ""
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_new_native_delivery_id_is_not_suppressed(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.session import SessionSource

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    old_hash = hashlib.sha256(b"old-trigger").hexdigest()
    (tmp_path / ".restart_last_processed.json").write_text(json.dumps({
        "platform": "slack",
        "delivery_id_hash": old_hash,
        "requested_at": time.time(),
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.SLACK, chat_id="C1", chat_type="group", user_id="U1"),
        platform_delivery_id="new-trigger",
    )

    await runner._handle_restart_command(event)

    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_restart_marker_hashes_native_delivery_id(tmp_path, monkeypatch):
    """Do not persist Slack's short-lived trigger capability verbatim."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    raw_id = "sensitive-short-lived-trigger"
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.SLACK, chat_id="C1", chat_type="group", user_id="U1"),
        platform_delivery_id=raw_id,
    )

    await runner._handle_restart_command(event)

    marker_text = (tmp_path / ".restart_last_processed.json").read_text()
    marker = json.loads(marker_text)
    assert raw_id not in marker_text
    assert marker["delivery_id_hash"] == hashlib.sha256(raw_id.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_different_platform_bypasses_dedup(tmp_path, monkeypatch):
    """Marker from Telegram doesn't block a /restart from another platform."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 12345,
        "requested_at": time.time(),
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    # /restart from Discord — not a redelivery candidate
    discord_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="discord-chan",
        chat_type="dm",
        user_id="u1",
    )
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=discord_source,
        message_id="m1",
        platform_update_id=12345,
    )
    await runner._handle_restart_command(event)

    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_marker_missing_but_booted_from_restart_ignores_redelivery(tmp_path, monkeypatch):
    """Missing marker + just booted from a /restart + young process → treat as stale.

    Reproduces the infinite-loop scenario (issue #18528): the dedup marker went
    missing, so the update_id comparison can't run. Because this process booted
    from a chat-originated /restart and is still within the post-boot window,
    the redelivered /restart is suppressed instead of re-restarting the gateway.
    """
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    runner._booted_from_restart = True
    runner._startup_time = time.time()

    event = _make_restart_event(update_id=100)
    result = await runner._handle_restart_command(event)

    assert result == ""  # silently ignored
    runner.request_restart.assert_not_called()
    # One-shot: the flag is consumed so a later legitimate /restart is honored.
    assert runner._booted_from_restart is False



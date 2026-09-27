"""Tests for /restart idempotency across platform re-delivery.

Telegram can re-deliver an update after its graceful-shutdown ACK fails; native
Slack/Discord slash interactions can likewise arrive again without a message
identity. The durable guard must recognize the same ingress after process
restart without swallowing a genuinely new /restart.
"""
import hashlib
import json
import time
from unittest.mock import MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
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
async def test_event_without_replay_identity_bypasses_dedup(tmp_path, monkeypatch):
    """Events with neither update nor platform-event identity aren't gated."""
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

    # No update_id or platform_event_id — the dedup check should NOT kick in.
    event = _make_restart_event(update_id=None)
    await runner._handle_restart_command(event)

    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_different_platform_bypasses_dedup(tmp_path, monkeypatch):
    """Marker from Telegram doesn't block a /restart from another platform."""
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


def _make_native_restart_event(platform: Platform, platform_event_id: str) -> MessageEvent:
    return MessageEvent(
        text="/restart",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=platform,
            chat_id="native-channel",
            chat_type="group",
            user_id="native-user",
        ),
        platform_event_id=platform_event_id,
    )


@pytest.mark.asyncio
async def test_native_restart_replay_uses_hashed_platform_event_identity(tmp_path, monkeypatch):
    """A native slash replay survives process restart without persisting Slack's raw trigger id."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    trigger_id = "13345224609.738474920.8088930838d88f008e0"
    event = _make_native_restart_event(Platform.SLACK, trigger_id)

    first, _adapter = make_restart_runner()
    first.request_restart = MagicMock(return_value=True)
    await first._handle_restart_command(event)

    marker_text = (tmp_path / ".restart_last_processed.json").read_text(encoding="utf-8")
    marker = json.loads(marker_text)
    assert trigger_id not in marker_text
    assert marker["platform"] == "slack"
    assert marker["platform_event_id_sha256"] == hashlib.sha256(trigger_id.encode()).hexdigest()
    assert "update_id" not in marker

    replay, _adapter = make_restart_runner()
    replay.request_restart = MagicMock(return_value=True)
    result = await replay._handle_restart_command(
        _make_native_restart_event(Platform.SLACK, trigger_id)
    )

    assert result == ""
    replay.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_native_restart_identity_is_not_swallowed(tmp_path, monkeypatch):
    """A later native slash with a different interaction identity is a new command."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "discord",
        "platform_event_id_sha256": hashlib.sha256(b"111").hexdigest(),
        "requested_at": time.time(),
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    await runner._handle_restart_command(
        _make_native_restart_event(Platform.DISCORD, "222")
    )

    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_native_restart_identity_is_platform_scoped(tmp_path, monkeypatch):
    """The same opaque identity on another platform cannot match the marker."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "slack",
        "platform_event_id_sha256": hashlib.sha256(b"same").hexdigest(),
        "requested_at": time.time(),
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    await runner._handle_restart_command(
        _make_native_restart_event(Platform.DISCORD, "same")
    )

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



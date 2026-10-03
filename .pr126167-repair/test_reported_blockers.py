"""Independent execution of @andrexibiza's two reported lifecycle counterexamples.

External diagnostic only; this file is not part of the proposed source commit.
Platform sends and model calls are excluded; queue, timer, spool, command writer
and prompt-pin owners are the product implementations.
"""
import asyncio
import dataclasses
import json

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from tests.gateway.test_internal_event_pin_wiring import (
    KEY, _capture, _drive, _human_source, _make_runner,
)


class OfflineAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, *args, **kwargs):
        raise AssertionError("No platform sends in this diagnostic")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


@pytest.mark.asyncio
@pytest.mark.parametrize("synthetic_head", [False, True], ids=["human-control", "synthetic-head"])
async def test_accepted_debounce_survives_shutdown(monkeypatch, synthetic_head):
    from hermes_constants import get_hermes_home

    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")
    adapter = OfflineAdapter(PlatformConfig(enabled=True, token="fixture"), Platform.TELEGRAM)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner.adapters, runner._profile_adapters = {Platform.TELEGRAM: adapter}, {}
    runner._primary_profile_name, runner._sessions, runner._draining = "default", {}, False
    runner._busy_input_mode = runner._busy_text_mode = adapter._busy_text_mode = "queue"
    adapter.gateway_runner = runner
    adapter._busy_session_handler = runner._handle_active_session_busy_message

    async def held_turn(event):
        raise AssertionError("The active turn must remain in backoff")

    adapter.set_message_handler(held_turn)
    source = adapter.build_source(chat_id="1001", chat_type="dm", user_id="101", message_id="201")
    head = runner._synthetic_prompt_event(source, "pending-continuation") if synthetic_head else MessageEvent(
        text="pending-human", source=source, message_id="201")
    key = adapter._event_session_key(head)
    adapter._active_sessions[key] = asyncio.Event()
    runner._enqueue_fifo(key, head, adapter)
    adapter._spawn_drain_task(head, key, delay=adapter._REQUEUE_BACKOFF_MAX_SECONDS)
    human = MessageEvent(text="human-must-survive-shutdown", source=adapter.build_source(
        chat_id="1001", chat_type="dm", user_id="101", message_id="202"), message_id="202")
    try:
        await adapter.handle_message(human)
        assert key in adapter._text_debounce
        await asyncio.wait_for(adapter._text_debounce[key].task, timeout=5)
        assert human._gateway_accepted
        assert not adapter._session_tasks[key].done()
    finally:
        await adapter.cancel_background_tasks()
    payloads = [json.loads(p.read_text()) for p in (get_hermes_home() / "pending_messages").glob("*.json")]
    print("SHUTDOWN", synthetic_head, payloads)
    assert "human-must-survive-shutdown" in json.dumps(payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("change_home", [False, True], ids=["unchanged-control", "home-reconfigured"])
async def test_synthetic_turn_observes_authoritative_home_change(monkeypatch, change_home):
    config = GatewayConfig()
    config.platforms[Platform.DISCORD] = PlatformConfig(enabled=True, token="fixture", home_channel=HomeChannel(
        platform=Platform.DISCORD, chat_id="111111111111111111", name="Old home"))
    runner = _make_runner(monkeypatch, config)
    calls = []
    _capture(runner, calls)
    source = _human_source()
    await _drive(runner, ((False, source),), channel_prompt="Channel hint.")
    expected = "222222222222222222" if change_home else "111111111111111111"
    if change_home:
        command = MessageEvent(text="/sethome", source=dataclasses.replace(
            source, chat_id=expected, chat_name="New home"))
        await runner._handle_set_home_command(command)
        assert config.platforms[Platform.DISCORD].home_channel.chat_id == expected
    synthetic = runner._synthetic_prompt_event(source, "[heartbeat] continue")
    await runner._handle_message_with_agent(synthetic, synthetic.source, KEY, 1)
    await _drive(runner, ((False, source),), channel_prompt="Channel hint.")
    seen = [call["context_prompt"] for call in calls]
    assert len(seen) == 3
    print("HOME", change_home, seen)
    assert expected in seen[2]
    assert expected in seen[1]

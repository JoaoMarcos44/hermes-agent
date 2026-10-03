"""Regression scenarios reported in #126167: teardown ownership and live config.

The provider and transport are offline. Admission, timers, drain cancellation,
spooling, the slash-command writer and prompt owners are the product methods.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
import inspect

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_prompt_pin import sanitize_prompt_pin
from tests.gateway.test_internal_event_pin_wiring import (
    KEY, _capture, _drive, _human_source, _make_runner, _wake_source,
)


class OfflineAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, *args, **kwargs):
        raise AssertionError("This regression must not send a platform message")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


@pytest.mark.asyncio
@pytest.mark.parametrize("synthetic_head", [False, True], ids=["human", "synthetic"])
@pytest.mark.parametrize("queue_depth", [1, 32], ids=["one", "full"])
@pytest.mark.parametrize("expire", [False, True], ids=["unfired", "expired"])
async def test_teardown_spools_accepted_debounce_independently(
    monkeypatch, synthetic_head, queue_depth, expire,
):
    from hermes_constants import get_hermes_home

    assert Path(inspect.getfile(BasePlatformAdapter)).resolve().parents[2] == Path(__file__).resolve().parents[2]
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    adapter = OfflineAdapter(PlatformConfig(enabled=True, token="fixture"), Platform.TELEGRAM)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner.adapters, runner._profile_adapters = {Platform.TELEGRAM: adapter}, {}
    runner._primary_profile_name = "default"
    runner._sessions, runner._draining = {}, False
    runner._busy_input_mode = runner._busy_text_mode = adapter._busy_text_mode = "queue"
    adapter.gateway_runner = runner
    adapter._busy_session_handler = runner._handle_active_session_busy_message

    async def held_turn(event):
        raise AssertionError("The backed-off drain must not reach the model")

    adapter.set_message_handler(held_turn)
    source = adapter.build_source(chat_id="1001", chat_type="dm", user_id="101", message_id="201")
    head = (
        runner._synthetic_prompt_event(source, "pending-continuation")
        if synthetic_head else MessageEvent(text="pending-human", source=source, message_id="201")
    )
    key = adapter._event_session_key(head)
    adapter._active_sessions[key] = asyncio.Event()
    runner._enqueue_fifo(key, head, adapter)
    for index in range(1, queue_depth):
        runner._enqueue_fifo(
            key, MessageEvent(text=f"older-fifo-{index}", source=source), adapter,
        )
    adapter._spawn_drain_task(head, key, delay=adapter._REQUEUE_BACKOFF_MAX_SECONDS)
    human = MessageEvent(
        text="accepted-human-must-survive", message_id="202",
        source=adapter.build_source(chat_id="1001", chat_type="dm", user_id="101", message_id="202"),
    )
    spool = get_hermes_home() / "pending_messages"
    prior = set(spool.glob("*.json"))
    try:
        await adapter.handle_message(human)
        assert human._gateway_accepted
        assert key in adapter._text_debounce
        if expire:
            await asyncio.wait_for(adapter._text_debounce[key].task, timeout=5)
        assert not adapter._session_tasks[key].done()
        await adapter.cancel_background_tasks()
        payloads = [json.loads(path.read_text()) for path in set(spool.glob("*.json")) - prior]
        texts = [payload["data"]["text"] for payload in payloads]
        assert sum("accepted-human-must-survive" in text for text in texts) == 1, texts
        assert any(head.text in text for text in texts), texts
        assert all(payload["session_key"] == key for payload in payloads)
        assert not adapter._text_debounce
        # A second teardown must not duplicate already spooled events.
        saved = set(spool.glob("*.json"))
        await adapter.cancel_background_tasks()
        assert set(spool.glob("*.json")) == saved
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("change_home", [False, True], ids=["unchanged", "sethome"])
@pytest.mark.parametrize("restart", ["live", "restored", "legacy"])
@pytest.mark.parametrize("sparse", [False, True], ids=["full-source", "minimal-source"])
@pytest.mark.parametrize("internal", [False, True], ids=["continuation", "internal"])
async def test_preserved_source_observes_current_config(
    monkeypatch, tmp_path, change_home, restart, sparse, internal,
):
    assert Path(inspect.getfile(GatewayRunner)).resolve().parents[1] == Path(__file__).resolve().parents[2]
    old_id, new_id = "111111111111111111", "222222222222222222"
    config = GatewayConfig()
    config.platforms[Platform.DISCORD] = PlatformConfig(
        enabled=True, token="fixture",
        home_channel=HomeChannel(platform=Platform.DISCORD, chat_id=old_id, name="Old home"),
    )
    durable = {}
    runner = _make_runner(monkeypatch, config, durable_prompt_pin=durable)
    source = _human_source()
    before = []
    _capture(runner, before)
    await _drive(runner, ((False, source),), channel_prompt="Channel hint.")
    assert old_id in before[0]["context_prompt"]
    if change_home:
        command = MessageEvent(
            text="/sethome",
            source=dataclasses.replace(source, chat_id=new_id, chat_name="New home"),
        )
        await runner._handle_set_home_command(command)
        assert config.platforms[Platform.DISCORD].home_channel.chat_id == new_id
    if restart != "live":
        # Exercise the actual snapshot sanitizer and JSON round trip, rather
        # than transporting a Python object directly between the two runners.
        saved = sanitize_prompt_pin(durable["value"])
        assert saved is not None
        if restart == "legacy":
            saved.pop("context_source", None)
            saved.pop("shared_multi_user_session", None)
        path = tmp_path / "prompt-pin.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        durable["value"] = json.loads(path.read_text(encoding="utf-8"))
        runner = _make_runner(monkeypatch, config, durable_prompt_pin=durable)
    calls = []
    _capture(runner, calls)
    event = runner._synthetic_prompt_event(_wake_source() if sparse else source, "[heartbeat] continue")
    if internal:
        event = dataclasses.replace(event, internal=True)
    wire_source = event.source.to_dict()
    await runner._handle_message_with_agent(event, event.source, KEY, 1)
    await _drive(runner, ((False, source),), channel_prompt="Channel hint.")
    assert len(calls) == 2
    expected = new_id if change_home else old_id
    assert expected in calls[0]["context_prompt"]
    assert expected in calls[1]["context_prompt"]
    assert [call["channel_prompt"] for call in calls] == ["Channel hint."] * 2
    assert event.source.to_dict() == wire_source, "display pin must not rewrite routing source"
    assert calls[0]["source"].message_id is None
    if change_home:
        assert old_id not in calls[0]["context_prompt"]
    if restart != "legacy":
        assert calls[0]["context_prompt"] == calls[1]["context_prompt"]
        assert "Guild / #general" in calls[0]["context_prompt"]
        if not change_home:
            assert calls[0]["context_prompt"] == before[0]["context_prompt"]

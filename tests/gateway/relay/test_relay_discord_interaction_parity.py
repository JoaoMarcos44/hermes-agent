"""Relayed Discord messages and interactions must share one prompt/session identity."""

import json
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.relay.ws_transport import _event_from_wire
from gateway.session import SessionStore, build_session_context, build_session_key
from tests.gateway.relay.test_relay_interactive import _adapter


def _forward(**payload):
    body = {
        "type": 2,
        "id": "i1",
        "channel_id": "ch1",
        "guild_id": "g1",
        "data": {"name": "status"},
    }
    body.update(payload)

    class Forward:
        platform = "discord"
        method = "POST"
        path = "/interactions/bot1"

    Forward.body = json.dumps(body).encode()
    return Forward()


def _message(
    *, chat_name="Hermes / #ops", chat_topic="triage", thread=False,
    user_id="u1", user_name="ben", user_display_name="Ben D",
):
    chat = (
        {"chat_id": "th1", "chat_type": "thread", "thread_id": "th1", "parent_chat_id": "ch1"}
        if thread else {"chat_id": "ch1", "chat_type": "group"}
    )
    return _event_from_wire({
        "text": "hello",
        "message_type": "text",
        "source": {
            "platform": "discord",
            **chat,
            "scope_id": "g1",
            "user_id": user_id,
            "user_name": user_name,
            "user_display_name": user_display_name,
            "chat_name": chat_name,
            "chat_topic": chat_topic,
            "message_id": "m1",
        },
    })


def _prompt_sequence(*sources):
    runner = object.__new__(gateway_run.GatewayRunner)
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    return [
        runner._pinned_session_context_prompt(build_session_context(source, config), False, "same-session")
        for source in sources
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [False, True], ids=["channel", "thread"])
async def test_message_interaction_message_keeps_prompt_and_session_identity(thread):
    adapter, _stub = _adapter(platform="discord")
    adapter.handle_message = AsyncMock()
    message = _message(thread=thread)
    await adapter._on_inbound(message)

    interaction = {
        "member": {
            "nick": "Ben D",
            "user": {"id": "u1", "username": "ben", "global_name": "Ben D"},
        },
    }
    if thread:
        interaction.update(
            channel_id="th1",
            channel={"id": "th1", "type": 11, "parent_id": "ch1"},
        )
    slash = adapter._discord_interaction_to_event(_forward(**interaction))

    assert slash is not None
    assert build_session_key(slash.source) == build_session_key(message.source)
    assert (slash.source.chat_name, slash.source.chat_topic, slash.source.user_name) == (
        message.source.chat_name,
        message.source.chat_topic,
        message.source.user_name,
    )
    assert len(set(_prompt_sequence(message.source, slash.source, message.source))) == 1


@pytest.mark.asyncio
async def test_channel_rename_survives_restart_with_latest_text_lane_labels(tmp_path):
    """A reused session's creation-time origin must not win after newer labels were observed."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    old = _message(chat_name="Hermes / #ops-old", chat_topic="old topic")
    store.get_or_create_session(old.source)

    adapter, _stub = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()
    await adapter._on_inbound(old)

    renamed = _message(chat_name="Hermes / #ops", chat_topic="new topic")
    await adapter._on_inbound(renamed)
    store.close_all_db_handles()

    restarted_store = SessionStore(tmp_path, config)
    restarted, _stub = _adapter(platform="discord")
    restarted.set_session_store(restarted_store)
    slash = restarted._discord_interaction_to_event(_forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))

    assert slash is not None
    assert (slash.source.chat_name, slash.source.chat_topic) == (
        renamed.source.chat_name,
        renamed.source.chat_topic,
    )
    assert len(set(_prompt_sequence(renamed.source, slash.source, renamed.source))) == 1

    # /new/reset replaces the entry and drops generic metadata, but inherits the refreshed origin.
    key = build_session_key(renamed.source)
    reset_entry = restarted_store.reset_session(key)
    assert reset_entry is not None
    restarted_store.close_all_db_handles()
    after_reset = SessionStore(tmp_path, config)
    reset_adapter, _stub = _adapter(platform="discord")
    reset_adapter.set_session_store(after_reset)
    slash_after_reset = reset_adapter._discord_interaction_to_event(_forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))
    assert slash_after_reset is not None
    assert (slash_after_reset.source.chat_name, slash_after_reset.source.chat_topic) == (
        renamed.source.chat_name,
        renamed.source.chat_topic,
    )


@pytest.mark.asyncio
async def test_latest_channel_labels_win_across_per_user_sessions_after_restart(tmp_path):
    """One user's rename observation must supersede another user's older session origin."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    alice = _message(chat_name="Hermes / #before", user_id="u1", user_display_name="Alice")
    bob = _message(chat_name="Hermes / #before", user_id="u2", user_name="bob", user_display_name="Bob")
    store.get_or_create_session(alice.source)
    store.get_or_create_session(bob.source)

    adapter, _stub = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()
    await adapter._on_inbound(alice)
    await adapter._on_inbound(bob)

    renamed = _message(
        chat_name="Hermes / #after", chat_topic="renamed",
        user_id="u1", user_display_name="Alice",
    )
    await adapter._on_inbound(renamed)
    store.close_all_db_handles()

    restarted_store = SessionStore(tmp_path, config)
    restarted, _stub = _adapter(platform="discord")
    restarted.set_session_store(restarted_store)
    slash = restarted._discord_interaction_to_event(_forward(
        member={"user": {"id": "u2", "username": "bob", "global_name": "Bob"}},
    ))

    assert slash is not None
    assert (slash.source.chat_name, slash.source.chat_topic) == (
        renamed.source.chat_name, renamed.source.chat_topic,
    )


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ({"nick": "Benny", "user": {"id": "u1", "username": "ben", "global_name": "Ben D"}}, "Benny"),
        ({"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}}, "Ben D"),
        ({"user": {"id": "u1", "username": "ben"}}, "ben"),
    ],
)
def test_cold_interaction_uses_discord_display_name_order(member, expected):
    adapter, _stub = _adapter(platform="discord")
    event = adapter._discord_interaction_to_event(_forward(member=member))
    assert event is not None
    assert event.source.user_name == expected

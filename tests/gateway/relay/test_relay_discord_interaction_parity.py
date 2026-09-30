"""Relayed Discord messages and interactions must share one prompt/session identity."""

import json
import threading
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


async def _passthrough_event(adapter, forward):
    adapter.handle_message = AsyncMock()
    await adapter._on_passthrough(forward)
    assert adapter.handle_message.await_count == 1
    return adapter.handle_message.await_args.args[0]


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
    slash = await _passthrough_event(restarted, _forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))

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
    slash_after_reset = await _passthrough_event(reset_adapter, _forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))
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
    slash = await _passthrough_event(restarted, _forward(
        member={"user": {"id": "u2", "username": "bob", "global_name": "Bob"}},
    ))

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


@pytest.mark.asyncio
async def test_peer_reset_cannot_outvote_newer_channel_observation(tmp_path):
    """A reset inherits routing state; it must never manufacture a newer channel observation."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    adapter, _stub = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()

    alice = _message(chat_name="A", user_id="u1", user_display_name="Alice")
    bob = _message(chat_name="A", user_id="u2", user_name="bob", user_display_name="Bob")
    for event in (alice, bob):
        store.get_or_create_session(event.source)
        await adapter._on_inbound(event)

    renamed = _message(chat_name="B", user_id="u1", user_display_name="Alice")
    await adapter._on_inbound(renamed)
    store.reset_session(build_session_key(bob.source))
    store.close_all_db_handles()

    restarted_store = SessionStore(tmp_path, config)
    restarted, _stub = _adapter(platform="discord")
    restarted.set_session_store(restarted_store)
    slash = await _passthrough_event(restarted, _forward(
        member={"user": {"id": "u2", "username": "bob", "global_name": "Bob"}},
    ))
    assert slash.source.chat_name == "B"


@pytest.mark.asyncio
async def test_latest_equal_peer_value_survives_restart(tmp_path):
    """A -> B -> A must persist the final A even when that observer's older value was also A."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    adapter, _stub = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()

    for uid in ("u1", "u2"):
        event = _message(chat_name="A", user_id=uid, user_display_name=uid)
        store.get_or_create_session(event.source)
        await adapter._on_inbound(event)
    await adapter._on_inbound(_message(chat_name="B", user_id="u1", user_display_name="u1"))
    await adapter._on_inbound(_message(chat_name="A", user_id="u2", user_display_name="u2"))
    store.close_all_db_handles()

    restarted_store = SessionStore(tmp_path, config)
    restarted, _stub = _adapter(platform="discord")
    restarted.set_session_store(restarted_store)
    slash = await _passthrough_event(restarted, _forward(
        member={"user": {"id": "u2", "username": "u2", "global_name": "u2"}},
    ))
    assert slash.source.chat_name == "A"


@pytest.mark.asyncio
async def test_label_observation_never_replaces_session_origin(tmp_path):
    """The context cache is independent of routing provenance stored on SessionEntry.origin."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    original = _message(chat_name="A")
    entry = store.get_or_create_session(original.source)
    origin = entry.origin
    marker = object()
    origin._transport_adapter_ref = marker

    adapter, _stub = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()
    await adapter._on_inbound(_message(chat_name="B"))

    current = store._entries[entry.session_key]
    assert current.origin is origin
    assert current.origin._transport_adapter_ref is marker


@pytest.mark.asyncio
async def test_missing_nick_uses_known_name_but_explicit_null_invalidates_it(tmp_path):
    """Optional nick omission preserves known identity; explicit null proves nickname removal."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    adapter, _stub = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()
    await adapter._on_inbound(_message(user_display_name="Benny"))
    store.close_all_db_handles()

    restarted_store = SessionStore(tmp_path, config)
    restarted, _stub = _adapter(platform="discord")
    restarted.set_session_store(restarted_store)

    missing = await _passthrough_event(restarted, _forward(member={
        "user": {"id": "u1", "username": "ben", "global_name": "Ben D"},
    }))
    assert missing.source.user_name == "Benny"

    removed = await _passthrough_event(restarted, _forward(member={
        "nick": None,
        "user": {"id": "u1", "username": "ben", "global_name": "Ben D"},
    }))
    assert removed.source.user_name == "Ben D"
    restarted_store.close_all_db_handles()

    after = SessionStore(tmp_path, config)
    after_adapter, _stub = _adapter(platform="discord")
    after_adapter.set_session_store(after)
    missing_after_removal = await _passthrough_event(after_adapter, _forward(member={
        "user": {"id": "u1", "username": "ben", "global_name": "Ben D"},
    }))
    assert missing_after_removal.source.user_name == "Ben D"


@pytest.mark.asyncio
async def test_shared_store_invalidates_peer_adapter_context_cache(tmp_path):
    """A successful miss or hit in one adapter must see later observations from a peer adapter."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    reader, _ = _adapter(platform="discord")
    writer, _ = _adapter(platform="discord")
    reader.set_session_store(store)
    writer.set_session_store(store)
    writer.handle_message = AsyncMock()

    first = await _passthrough_event(reader, _forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))
    assert first.source.chat_name is None

    await writer._on_inbound(_message(chat_name="A"))
    second = await _passthrough_event(reader, _forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))
    assert second.source.chat_name == "A"

    await writer._on_inbound(_message(chat_name="B"))
    third = await _passthrough_event(reader, _forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))
    assert third.source.chat_name == "B"


@pytest.mark.asyncio
async def test_cold_context_read_runs_off_event_loop(tmp_path):
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    seed, _ = _adapter(platform="discord")
    seed.set_session_store(store)
    seed.handle_message = AsyncMock()
    await seed._on_inbound(_message(chat_name="A"))
    store.close_all_db_handles()

    cold_store = SessionStore(tmp_path, config)
    adapter, _ = _adapter(platform="discord")
    adapter.set_session_store(cold_store)
    loop_thread = threading.get_ident()
    read_threads = []
    original = cold_store.relay_discord_context

    def recording_read(*args):
        read_threads.append(threading.get_ident())
        return original(*args)

    cold_store.relay_discord_context = recording_read
    event = await _passthrough_event(adapter, _forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))
    assert event.source.chat_name == "A"
    assert read_threads and all(thread_id != loop_thread for thread_id in read_threads)


@pytest.mark.asyncio
async def test_partial_thread_channel_recovers_known_parent(tmp_path):
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    adapter, _ = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()
    message = _message(thread=True)
    await adapter._on_inbound(message)

    interaction = await _passthrough_event(adapter, _forward(
        channel_id="th1",
        channel={"id": "th1", "type": 11},
        member={"nick": "Ben D", "user": {"id": "u1", "username": "ben"}},
    ))
    assert interaction.source.parent_chat_id == "ch1"
    assert build_session_key(interaction.source) == build_session_key(message.source)


def test_message_identity_is_normalized_at_each_ingress_boundary():
    text = _event_from_wire({
        "text": "hello",
        "message_type": "text",
        "message_id": "platform-message-1",
        "source": {
            "platform": "discord",
            "chat_id": "ch1",
            "chat_type": "group",
            "scope_id": "g1",
            "user_id": "u1",
        },
    })
    assert text.message_id == "platform-message-1"
    assert text.source.message_id == "platform-message-1"

    adapter, _ = _adapter(platform="discord")
    component = adapter._discord_interaction_to_event(_forward(
        type=3,
        id="interaction-1",
        message={"id": "actual-message-77"},
        member={"user": {"id": "u1", "username": "ben"}},
        data={"custom_id": "foreign-button"},
    ))
    assert component is not None
    assert component.message_id == component.source.message_id == "actual-message-77"
    assert component.metadata["discord_interaction_id"] == "interaction-1"

    slash = adapter._discord_interaction_to_event(_forward(
        type=2,
        id="interaction-2",
        member={"user": {"id": "u1", "username": "ben"}},
        data={"name": "status"},
    ))
    assert slash is not None
    assert slash.message_id is None and slash.source.message_id is None
    assert slash.metadata["discord_interaction_id"] == "interaction-2"


@pytest.mark.asyncio
async def test_topic_removal_is_an_authoritative_observation(tmp_path):
    """A removed Discord topic must not revive from durable context on the next interaction."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    adapter, _ = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()

    await adapter._on_inbound(_message(chat_topic="old topic"))
    await adapter._on_inbound(_message(chat_topic=None))
    store.close_all_db_handles()

    restarted_store = SessionStore(tmp_path, config)
    restarted, _ = _adapter(platform="discord")
    restarted.set_session_store(restarted_store)
    slash = await _passthrough_event(restarted, _forward(
        member={"user": {"id": "u1", "username": "ben", "global_name": "Ben D"}},
    ))
    assert slash.source.chat_topic is None


@pytest.mark.asyncio
async def test_unchanged_text_context_skips_worker_round_trip(tmp_path, monkeypatch):
    """After publication, repeated identical text messages take the lock-free fast path."""
    config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")})
    store = SessionStore(tmp_path, config)
    adapter, _ = _adapter(platform="discord")
    adapter.set_session_store(store)
    adapter.handle_message = AsyncMock()
    first = _message()
    await adapter._on_inbound(first)

    calls = 0
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(func, *args, **kwargs):
        nonlocal calls
        if getattr(func, "__name__", "") == "observe_relay_discord_context":
            calls += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)
    repeat = _message()
    repeat.source.message_id = "m2"
    repeat.message_id = "m2"
    await adapter._on_inbound(repeat)
    assert calls == 0

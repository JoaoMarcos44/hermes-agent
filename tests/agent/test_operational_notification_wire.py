"""Operational gateway events are not human user turns on the provider wire."""

import copy
import time
from types import SimpleNamespace

from agent.turn_context import _stage_turn_user_message, build_api_messages
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.response_filters import display_kind_for_event
from gateway.session import SessionSource


_NOTIFICATION_TEXT = "✔ Kanban T-42 done — worker summary"


def _request_agent():
    """Provide the narrow state read by the real provider-message builder."""
    return SimpleNamespace(
        _current_turn_timestamp=time.time(),
        ephemeral_system_prompt=None,
        _should_sanitize_tool_calls=lambda: False,
        _copy_reasoning_content_for_api=lambda _message, _api_message: None,
    )


def _stage_event(event, *, display_metadata=None):
    message, _pending = _stage_turn_user_message(
        SimpleNamespace(_pending_cli_user_message=None),
        event.text,
        None,
        None,
        None,
        display_kind_for_event(event),
        display_metadata if display_metadata is not None else event.metadata,
    )
    return message


def _provider_messages(transcript, current_turn_user_idx, *, plugin_user_context=""):
    return build_api_messages(
        _request_agent(),
        transcript,
        current_turn_user_idx=current_turn_user_idx,
        ext_prefetch_cache=None,
        plugin_user_context=plugin_user_context,
        moa_config=None,
        active_system_prompt="stable system prompt",
    )[0]


def test_internal_notification_is_not_a_provider_user_turn_and_transcript_is_unchanged(
    _isolate_hermes_home,
):
    event = MessageEvent(
        text=_NOTIFICATION_TEXT,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="dm-1"),
        internal=True,
        metadata={"notification_origin": "process_registry_synthetic"},
    )
    notification = _stage_event(event)
    legacy_notification = {
        "role": "user",
        "content": "Previously delivered internal notification",
        "display_kind": "internal_notification",
    }
    transcript = [
        {
            "role": "user",
            "content": "Earlier human request",
            "api_content": "the cached bytes sent for the earlier request",
        },
        {"role": "assistant", "content": "Earlier answer"},
        legacy_notification,
        {"role": "assistant", "content": "Earlier notification reply"},
        notification,
    ]
    original_transcript = copy.deepcopy(transcript)

    explicit_context = "[Explicit recall: preserve the operator's pinned release constraint]"
    api_messages = _provider_messages(
        transcript, current_turn_user_idx=4, plugin_user_context=explicit_context
    )

    assert api_messages[0] == {"role": "system", "content": "stable system prompt"}
    assert api_messages[1] == {
        "role": "user",
        "content": "the cached bytes sent for the earlier request",
    }
    assert api_messages[3] == {
        "role": "user",
        "content": "Previously delivered internal notification",
    }
    assert any(
        message.get("role") == "user" and explicit_context in message.get("content", "")
        for message in api_messages
    ), f"explicit user-context recall was lost: {api_messages!r}"
    assert not any(
        message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and _NOTIFICATION_TEXT in message.get("content", "")
        for message in api_messages
    ), f"operational notification reached the provider as a user turn: {api_messages!r}"
    assert transcript == original_transcript


def test_real_user_and_scheduled_heartbeat_prompts_still_reach_the_provider(
    _isolate_hermes_home,
):
    real_user = MessageEvent(
        text=_NOTIFICATION_TEXT,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="dm-1"),
    )
    user_api_messages = _provider_messages(
        [_stage_event(real_user)], current_turn_user_idx=0
    )
    assert any(
        message.get("role") == "user" and message.get("content") == _NOTIFICATION_TEXT
        for message in user_api_messages
    )

    heartbeat = MessageEvent(
        text="Check the scheduled status now",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="dm-1"),
    )
    heartbeat._heartbeat_session_id = "session-1"
    heartbeat_api_messages = _provider_messages(
        [_stage_event(heartbeat, display_metadata={"scheduled_heartbeat": True})],
        current_turn_user_idx=0,
    )
    assert any(
        message.get("role") == "user"
        and message.get("content") == "Check the scheduled status now"
        for message in heartbeat_api_messages
    )

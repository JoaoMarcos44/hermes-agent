"""Regression tests for #106260.

A stream that delivered text and then failed because the request was already
past a context/payload limit must not seed that text as a continuation stub.
The stub is terminal instead, so the loop persists the unchanged transcript
and gives the user an actionable clean-session recovery instead of growing the
prompt on every retry.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.chat_completion_helpers import (
    _StreamingCall,
    _build_partial_stream_stub,
)
from agent.turn_retry_state import TurnRetryState
from hermes_constants import FINISH_REASON_LENGTH, PARTIAL_STREAM_STUB_ID


_OVERFLOW_ERRORS = (
    "Context length exceeded: max compression attempts (3) reached.",
    "Context length exceeded: 51,329 tokens. Cannot compress further.",
    "This model's maximum context length is 200000 tokens. However, your messages resulted in 201234 tokens.",
    "Request entity too large: 413",
)


def _make_call(error, *, partial_text="x" * 71239, partial_tool_names=None):
    call = _StreamingCall.__new__(_StreamingCall)
    call.agent = SimpleNamespace(
        _current_streamed_assistant_text=partial_text,
        provider="test-provider",
        model="test-model",
        _fire_stream_delta=MagicMock(),
    )
    call.result = {
        "error": RuntimeError(error),
        "partial_tool_names": partial_tool_names or [],
    }
    return call


def _make_agent():
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


def _make_stream_chunk(content=None):
    delta = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=None)
    return SimpleNamespace(choices=[choice], model=None, usage=None)


@pytest.mark.parametrize("error", _OVERFLOW_ERRORS)
def test_overflow_partial_stub_is_terminal_and_drops_recovered_text(error):
    call = _make_call(error)

    stub = call._partial_stream_stub()

    assert stub.id == PARTIAL_STREAM_STUB_ID
    assert stub.choices[0].finish_reason == FINISH_REASON_LENGTH
    assert stub.choices[0].message.content is None
    assert stub._overflow_terminal is True


def test_overflow_mid_tool_call_keeps_warning_but_not_recovered_text():
    call = _make_call(
        _OVERFLOW_ERRORS[0],
        partial_text="write preamble " + "y" * 50000,
        partial_tool_names=["write_file"],
    )

    stub = call._partial_stream_stub()

    assert stub.choices[0].message.content is None
    assert stub._overflow_terminal is True
    warning = call.agent._fire_stream_delta.call_args.args[0]
    assert "Stream stalled mid tool-call" in warning
    assert "write_file" in warning


def test_transient_partial_stub_preserves_existing_continuation_behavior():
    call = _make_call("Connection reset by peer", partial_text="partial answer")

    stub = call._partial_stream_stub()

    assert stub.choices[0].message.content == "partial answer"
    assert getattr(stub, "_overflow_terminal", False) is False


@patch("run_agent.AIAgent._create_request_openai_client")
@patch("run_agent.AIAgent._close_request_openai_client")
def test_stream_path_marks_context_overflow_terminal(
    _mock_close, mock_create, monkeypatch,
):
    def _overflowing_stream():
        yield _make_stream_chunk(content="partial answer")
        raise RuntimeError("This model's maximum context length is 128000 tokens")

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = lambda *args, **kwargs: _overflowing_stream()
    mock_create.return_value = mock_client

    agent = _make_agent()
    agent._current_streamed_assistant_text = "partial answer"
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")

    response = agent._interruptible_streaming_api_call({})

    assert response.id == PARTIAL_STREAM_STUB_ID
    assert response._overflow_terminal is True
    assert response.choices[0].message.content is None


def _terminal_agent():
    return SimpleNamespace(
        log_prefix="test: ",
        _vprint=MagicMock(),
        _flush_status_buffer=MagicMock(),
        _cleanup_task_resources=MagicMock(),
        _persist_session=MagicMock(),
    )


def _terminal_response():
    return SimpleNamespace(
        id=PARTIAL_STREAM_STUB_ID,
        _overflow_terminal=True,
        choices=[SimpleNamespace(
            index=0,
            message=SimpleNamespace(
                role="assistant",
                content=None,
                tool_calls=None,
                reasoning_content=None,
            ),
            finish_reason=FINISH_REASON_LENGTH,
        )],
    )


def test_recovery_ends_without_appending_fragment_or_nudge():
    from agent.turn_truncation import _CONTEXT_OVERFLOW_PARTIAL_FINAL, recover_from_truncation

    agent = _terminal_agent()
    retry = TurnRetryState()
    messages = [{"role": "user", "content": "ask"}]

    verdict = recover_from_truncation(
        agent,
        _terminal_response(),
        FINISH_REASON_LENGTH,
        retry,
        messages=messages,
        conversation_history=None,
        api_kwargs={},
        api_call_count=1,
        effective_task_id="task",
        current_turn_user_idx=0,
        length_continue_retries=0,
        truncated_response_parts=[],
        truncated_tool_call_retries=0,
        retry_count=0,
        compression_attempts=0,
    )

    assert verdict.action == "return"
    assert verdict.result is not None
    assert verdict.result["failed"] is True
    assert verdict.result["final_response"] == _CONTEXT_OVERFLOW_PARTIAL_FINAL
    assert verdict.result["messages"] == messages
    assert retry.restart_with_length_continuation is False
    agent._persist_session.assert_called_once_with(messages, None)

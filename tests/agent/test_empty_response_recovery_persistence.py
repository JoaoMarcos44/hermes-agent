"""Regression tests for empty-response recovery transcript persistence."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.turn_recovery import abort_turn_on_interrupt
from agent.turn_finalizer import _close_transcript_tail, _drop_transcript_scaffolding
from hermes_state import SessionDB
from run_agent import AIAgent


class _CapturingSessionDB:
    """Minimal SessionDB stand-in that records every appended message."""

    def __init__(self):
        self.rows = []

    def append_message(self, session_id, role, content=None, **kwargs):
        self.rows.append({"role": role, "content": content})
        return len(self.rows)

    def append_messages_batch(self, session_id, messages, **kwargs):
        # Mirror the real batch writer: same rows, one call.
        for m in messages:
            self.rows.append({"role": m.get("role"), "content": m.get("content")})
        return list(range(len(self.rows) - len(messages) + 1, len(self.rows) + 1))


def _agent_with_capturing_db():
    agent = AIAgent.__new__(AIAgent)
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._session_db = _CapturingSessionDB()
    agent._session_db_created = True
    agent._last_flushed_db_idx = 0
    agent.session_id = "sess-test"
    return agent


def _agent_with_stubbed_persistence():
    agent = AIAgent.__new__(AIAgent)
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._session_db = None
    agent._session_messages = []
    agent.flushed_session_db_messages = []
    agent._flush_messages_to_session_db = lambda messages, conversation_history=None: (
        agent.flushed_session_db_messages.append([m.copy() for m in messages])
    )
    return agent


def test_persist_session_strips_scaffolding_and_closes_exposed_tool_tail():
    """Persistence removes request-local recovery rows without erasing executed tools.

    When cleanup exposes the executed tool result, the shared persistence boundary closes
    it so every early-exit caller returns an alternation-safe live history.
    """
    agent = _agent_with_stubbed_persistence()
    messages = [
        {"role": "user", "content": "run the task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "{}", "tool_call_id": "call_1"},
        {
            "role": "assistant",
            "content": "(empty)",
            "_empty_recovery_synthetic": True,
        },
        {
            "role": "user",
            "content": (
                "You just executed tool calls but returned an empty response. "
                "Please process the tool results above and continue with the task."
            ),
            "_empty_recovery_synthetic": True,
        },
    ]

    AIAgent._persist_session(agent, messages, conversation_history=[])

    assert [msg["role"] for msg in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"] == "call_1"
    assert messages[-1]["content"] == "Operation interrupted."
    assert agent.flushed_session_db_messages[-1] == messages
    assert all(not msg.get("_empty_recovery_synthetic") for msg in messages)


def _tool_then_empty_nudge():
    return [
        {"role": "user", "content": "run the task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "write_file", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "{\"ok\": true}", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "(empty)", "_empty_recovery_synthetic": True},
        {
            "role": "user",
            "content": "Please process the tool result and continue.",
            "_empty_recovery_synthetic": True,
        },
    ]


def test_empty_retry_interrupt_closes_with_the_real_interrupt_reason():
    """The durable close must preserve the exit owner's specific interrupt text.

    Closing before scaffold cleanup sees the synthetic user nudge and is a no-op.
    Persistence must therefore carry the exit owner's text through scaffold cleanup
    instead of replacing it with a generic close.
    """
    agent = _agent_with_stubbed_persistence()
    agent.log_prefix = ""
    agent._vprint = lambda *_args, **_kwargs: None
    agent.clear_interrupt = lambda **_kwargs: None
    messages = _tool_then_empty_nudge()
    interrupt_text = "Operation interrupted: retrying empty response from model (retry 1/3)."

    result = abort_turn_on_interrupt(
        agent,
        messages,
        conversation_history=[],
        api_call_count=2,
        abort_message="Interrupt detected during empty-response retry wait, aborting.",
        interrupt_text=interrupt_text,
    )

    assert [msg["role"] for msg in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"] == "call_1"
    assert messages[-1]["content"] == result["final_response"] == interrupt_text
    assert agent.flushed_session_db_messages[-1] == messages
    assert all(not msg.get("_empty_recovery_synthetic") for msg in messages)


def test_empty_give_up_closes_at_finalizer_without_losing_executed_tool():
    """The normal give-up path keeps the tool pair, then closes with delivered "(empty)".

    This proves the semantic close already has an owner; persistence does not need to
    manufacture one.
    """
    agent = _agent_with_stubbed_persistence()
    messages = _tool_then_empty_nudge()
    # Model the terminal sentinel produced by _terminal_empty after it first drops
    # the retry nudge.
    agent._drop_trailing_empty_response_scaffolding(messages)
    messages.append({
        "role": "assistant",
        "content": "(empty)",
        "_empty_terminal_sentinel": True,
    })

    _drop_transcript_scaffolding(agent, messages)
    _close_transcript_tail(agent, messages, "(empty)", False, False)
    AIAgent._persist_session(agent, messages, conversation_history=[])

    assert [msg["role"] for msg in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"] == "call_1"
    assert messages[-1]["content"] == "(empty)"
    assert agent.flushed_session_db_messages[-1] == messages


def test_persist_session_keeps_unmarked_terminal_empty_response():
    agent = _agent_with_stubbed_persistence()
    messages = [
        {"role": "user", "content": "run the task"},
        {"role": "assistant", "content": "(empty)"},
    ]

    AIAgent._persist_session(agent, messages, conversation_history=[])

    assert messages == [
        {"role": "user", "content": "run the task"},
        {"role": "assistant", "content": "(empty)"},
    ]
    assert agent.flushed_session_db_messages[-1] == messages






# Real turn-loop regressions for #120826: execute an actual file side effect,
# then drive empty-response recovery through the same AIAgent loop users hit.

_DEAD_LOCAL = "http://127.0.0.1:9"


def _response(content="", finish_reason="stop", tool_calls=None):
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls),
        finish_reason=finish_reason,
        index=0,
    )
    return SimpleNamespace(
        id="chatcmpl-empty-recovery",
        choices=[choice],
        model="test/model",
        usage=None,
    )


def _write_file_call(path):
    return SimpleNamespace(
        id="call_write",
        type="function",
        function=SimpleNamespace(
            name="write_file",
            arguments=json.dumps({
                "path": str(path),
                "content": "PAYMENT #1 SENT\n",
            }),
        ),
    )


@pytest.fixture
def real_empty_recovery_loop(tmp_path, monkeypatch):
    for var in (
        "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.setenv(var, _DEAD_LOCAL)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    monkeypatch.setattr("agent.title_generator.start_title_upgrade", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "sess-empty-recovery-root-cause"
    with patch("agent.process_bootstrap.OpenAI"), patch(
        "agent.model_metadata.fetch_model_metadata",
        return_value={},
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url=f"{_DEAD_LOCAL}/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=["file"],
            session_db=db,
            session_id=session_id,
        )

    def _no_real_client(*_args, **_kwargs):
        raise AssertionError("a real provider client would be built")

    agent._create_openai_client = _no_real_client
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False

    def run(script, user_message):
        pending = list(script)
        agent.client = MagicMock()

        def next_response(**kwargs):
            item = pending.pop(0)
            return item(**kwargs) if callable(item) else item

        agent.client.chat.completions.create.side_effect = next_response
        return agent.run_conversation(user_message)

    yield SimpleNamespace(
        agent=agent,
        db=db,
        session_id=session_id,
        ledger=tmp_path / "ledger.txt",
        run=run,
    )
    db.close()


def _tool_pair_ids(rows):
    calls = {
        tc["id"]
        for msg in rows
        if msg.get("role") == "assistant"
        for tc in (msg.get("tool_calls") or [])
    }
    results = {
        msg.get("tool_call_id")
        for msg in rows
        if msg.get("role") == "tool"
    }
    return calls, results


def _assert_executed_tool_pair_is_live_and_durable(loop, result):
    durable = loop.db.get_messages_as_conversation(loop.session_id)
    durable_calls, durable_results = _tool_pair_ids(durable)
    live_calls, live_results = _tool_pair_ids(result["messages"])

    assert "call_write" in durable_calls == durable_results
    assert durable_calls <= live_calls
    assert durable_results <= live_results
    assert result["messages"][-1]["role"] != "tool"
    assert durable[-1]["role"] != "tool"


def test_real_turn_empty_give_up_keeps_executed_write_in_next_model_context(
    real_empty_recovery_loop,
):
    loop = real_empty_recovery_loop
    first = loop.run(
        [
            _response(
                finish_reason="tool_calls",
                tool_calls=[_write_file_call(loop.ledger)],
            ),
            *[_response() for _ in range(8)],
        ],
        "record the payment in ledger.txt",
    )

    assert loop.ledger.read_text() == "PAYMENT #1 SENT\n"
    assert first["turn_exit_reason"] == "empty_response_exhausted"
    _assert_executed_tool_pair_is_live_and_durable(loop, first)

    seen_next_request = {}

    def answer_without_repeating_tool(**kwargs):
        sent = kwargs["messages"]
        seen_next_request["roles"] = [msg.get("role") for msg in sent]
        calls, results = _tool_pair_ids(sent)
        assert "call_write" in calls
        assert "call_write" in results
        return _response("The payment write already completed.")

    second = loop.run(
        [answer_without_repeating_tool],
        "did it work? if not, do it again",
    )

    assert second["final_response"] == "The payment write already completed."
    assert loop.ledger.read_text() == "PAYMENT #1 SENT\n"
    assert "tool" in seen_next_request["roles"]


def test_real_turn_stop_during_empty_retry_keeps_executed_write(
    real_empty_recovery_loop,
    monkeypatch,
):
    loop = real_empty_recovery_loop

    def interrupt_on_backoff(*_args, **_kwargs):
        loop.agent.interrupt("user pressed stop")
        return 5.0

    monkeypatch.setattr(
        "agent.retry_utils.jittered_backoff",
        interrupt_on_backoff,
    )

    result = loop.run(
        [
            _response(
                finish_reason="tool_calls",
                tool_calls=[_write_file_call(loop.ledger)],
            ),
            _response(),
            _response(),
        ],
        "record the payment in ledger.txt",
    )

    assert result["interrupted"] is True
    assert loop.ledger.read_text() == "PAYMENT #1 SENT\n"
    _assert_executed_tool_pair_is_live_and_durable(loop, result)
    assert result["messages"][-1]["content"].startswith(
        "Operation interrupted: retrying empty response"
    )


def test_flush_never_writes_buried_empty_recovery_scaffolding():
    """When an empty-after-tools nudge is followed by a tool-calling response,
    the synthetic ``(empty)`` + nudge pair stays buried in the live message
    list (only the trailing copies are ever dropped). The append-only flush
    must skip it regardless of position, otherwise the synthetic turns land in
    the session store and pollute every resumed transcript.
    """
    agent = _agent_with_capturing_db()

    messages = [
        {"role": "user", "content": "run the task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "{}", "tool_call_id": "call_1"},
        # Synthetic recovery scaffolding, now buried because the model answered
        # the nudge with another tool call rather than terminating.
        {"role": "assistant", "content": "(empty)", "_empty_recovery_synthetic": True},
        {
            "role": "user",
            "content": "You just executed tool calls but returned an empty response.",
            "_empty_recovery_synthetic": True,
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_2", "type": "function",
                            "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "{}", "tool_call_id": "call_2"},
        {"role": "assistant", "content": "All done."},
    ]

    agent._flush_messages_to_session_db(messages, conversation_history=[])

    persisted = agent._session_db.rows
    assert all(row["content"] != "(empty)" for row in persisted)
    assert all("empty response" not in (row["content"] or "") for row in persisted)
    # Only the genuine turns reach the store, in order.
    assert [r["role"] for r in persisted] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant",
    ]
    assert persisted[-1]["content"] == "All done."


def test_flush_skips_thinking_prefill_scaffolding():
    agent = _agent_with_capturing_db()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "_thinking_prefill": True},
        {"role": "assistant", "content": "Hello!"},
    ]
    agent._flush_messages_to_session_db(messages, conversation_history=[])

    assert [r["content"] for r in agent._session_db.rows] == ["hi", "Hello!"]

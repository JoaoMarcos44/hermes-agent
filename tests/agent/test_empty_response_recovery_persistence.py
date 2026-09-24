"""Regression tests for empty-response recovery transcript persistence."""

from agent.turn_recovery import abort_turn_on_interrupt
from agent.turn_finalizer import _close_transcript_tail, _drop_transcript_scaffolding
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


def test_persist_session_strips_only_trailing_empty_recovery_scaffolding():
    """Persistence removes request-local recovery rows without erasing executed tools.

    A generic persistence boundary is also used by shutdown and intermediate snapshots,
    so it must not synthesize a conversational close. Turn-exit owners close the tail.
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

    assert [msg["role"] for msg in messages] == ["user", "assistant", "tool"]
    assert messages[1]["tool_calls"][0]["id"] == messages[2]["tool_call_id"] == "call_1"
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
    """Regression beyond #120827: the close must preserve the exit owner's text.

    Closing before scaffold cleanup sees the synthetic user nudge and is a no-op.
    A generic persist-time close then loses the specific retry/Stop reason. This
    pins cleanup-before-close at the interrupt owner instead.
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

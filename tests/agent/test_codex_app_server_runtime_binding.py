"""Regression for a stale Codex thread binding after a gateway runtime switch."""

from pathlib import Path
from types import SimpleNamespace

from agent.transports import codex_app_server_session as session_module
from gateway.run_turn import GatewayTurnMixin
from hermes_state import SessionDB


SESSION_ID = "codex-runtime-binding"


class _AppServerTransport:
    """Protocol-boundary simulation; the real session, agent, and SessionDB remain in use."""

    clients = []
    next_thread_id = 0
    next_turn_id = 0

    def __init__(self, **_kwargs):
        self.requests = []
        self.notifications = []
        self.closed = False
        type(self).clients.append(self)

    def initialize(self, **_kwargs):
        return {}

    def request(self, method, params=None, timeout=30.0):
        params = dict(params or {})
        self.requests.append((method, params))
        if method == "thread/start":
            type(self).next_thread_id += 1
            self.thread_id = f"thread-{type(self).next_thread_id}"
            return {"thread": {"id": self.thread_id}}
        if method == "thread/resume":
            self.thread_id = params["threadId"]
            return {"thread": {"id": self.thread_id}}
        if method == "turn/start":
            type(self).next_turn_id += 1
            turn_id = f"turn-{type(self).next_turn_id}"
            self.notifications.extend(
                [
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "agentMessage",
                                "id": f"message-{turn_id}",
                                "text": f"reply-{turn_id}",
                            },
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turn": {"id": turn_id, "status": "completed", "error": None},
                        },
                    },
                ]
            )
            return {"turn": {"id": turn_id}}
        return {}

    def take_notification(self, timeout=0.0):
        if self.notifications:
            return self.notifications.pop(0)
        return None

    def take_server_request(self, timeout=0.0):
        return None

    def respond(self, *_args, **_kwargs):
        raise AssertionError("the scripted turns do not issue server requests")

    def respond_error(self, *_args, **_kwargs):
        raise AssertionError("the scripted turns do not issue server requests")

    def is_alive(self):
        return not self.closed

    def stderr_tail(self, _count=20):
        return []

    def close(self):
        self.closed = True


def _codex_agent(db):
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="transport-boundary-only",
        base_url="https://codex.invalid/v1",
        provider="openai",
        api_mode="codex_app_server",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_db=db,
        session_id=SESSION_ID,
    )
    agent._spawn_background_review = lambda **_kwargs: None
    return agent


def _sync_gateway_runtime(db, agent):
    # Exercise the production metadata writer against the real SessionDB.
    runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))
    GatewayTurnMixin._sync_session_model_from_agent(runner, SESSION_ID, agent)


def test_runtime_change_does_not_resume_a_thread_missing_intermediate_turns(monkeypatch, tmp_path):
    _AppServerTransport.clients = []
    _AppServerTransport.next_thread_id = 0
    _AppServerTransport.next_turn_id = 0
    monkeypatch.setattr(session_module, "CodexAppServerClient", _AppServerTransport)
    db = SessionDB(Path(tmp_path) / "state.db")
    db.create_session(session_id=SESSION_ID, source="api-server", model="codex")
    agents = []
    try:
        # First API-server lifetime: use the real AIAgent -> CodexAppServerSession path.
        first = _codex_agent(db)
        agents.append(first)
        first_result = first.run_conversation("Remember the word amber.")
        assert first_result["completed"] is True
        assert db.get_session_model_config_value(SESSION_ID, "codex_thread_id") == "thread-1"
        _sync_gateway_runtime(db, first)
        first.release_clients()

        # A real non-Codex turn updates the durable route marker and transcript,
        # but the current writer leaves the old app-server thread binding intact.
        db.append_message(SESSION_ID, "user", "What about the intermediate provider?")
        db.append_message(SESSION_ID, "assistant", "The intermediate answer is violet.")
        intermediate_agent = SimpleNamespace(
            model="claude-test",
            provider="anthropic",
            base_url="https://anthropic.invalid/v1",
            api_mode="chat_completions",
            _fallback_activated=False,
        )
        _sync_gateway_runtime(db, intermediate_agent)
        assert db.get_session_model_config_value(SESSION_ID, "gateway_runtime")["api_mode"] == "chat_completions"
        assert db.get_session_model_config_value(SESSION_ID, "codex_thread_id") == "thread-1"

        # Recreated API-server agent returns to Codex with the full durable transcript.
        # It must start a fresh provider thread seeded with the intervening turn, not
        # resume thread-1, whose model-side history cannot contain the Anthropic turn.
        recreated = _codex_agent(db)
        agents.append(recreated)
        history = db.get_messages(SESSION_ID)
        result = recreated.run_conversation("Continue after the switch.", conversation_history=history)
        assert result["completed"] is True

        thread_methods = [
            method
            for method, _params in _AppServerTransport.clients[-1].requests
            if method in {"thread/start", "thread/resume"}
        ]
        assert thread_methods == ["thread/start"]
        start_params = next(
            params
            for method, params in _AppServerTransport.clients[-1].requests
            if method == "thread/start"
        )
        assert "What about the intermediate provider?" in start_params["developerInstructions"]
        assert "The intermediate answer is violet." in start_params["developerInstructions"]
    finally:
        for agent in agents:
            agent.release_clients()
        db.close()

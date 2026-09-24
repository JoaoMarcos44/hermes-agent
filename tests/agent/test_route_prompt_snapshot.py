"""Route metadata commits must not destroy a valid stored prompt snapshot."""

from types import SimpleNamespace

import pytest

from agent.conversation_compression import _adopt_live_compression_child
from hermes_state import SessionDB


SESSION_ID = "route-prompt"


def _prompt(model: str = "model-a", provider: str = "openrouter") -> str:
    return (
        "You are Hermes Agent.\n\n"
        "Conversation started: Thursday, September 24, 2026\n"
        f"Model: {model}\n"
        f"Provider: {provider}"
    )


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


_ROUTE_WRITERS = (
    pytest.param(
        lambda db: db.update_session_model(
            SESSION_ID, "model-a", provider="openrouter", base_url="https://example.test/v1"
        ),
        id="model",
    ),
    pytest.param(
        lambda db: db.update_session_runtime_lock(
            SESSION_ID, model="model-a", provider="openrouter", confirmed=True
        ),
        id="runtime-lock",
    ),
    pytest.param(
        lambda db: db.update_session_billing_route(
            SESSION_ID, provider="openrouter", base_url="https://example.test/v1"
        ),
        id="billing-route",
    ),
)


@pytest.mark.parametrize("write_route", _ROUTE_WRITERS)
def test_route_writers_preserve_stored_prompt(db, write_route):
    prompt = _prompt()
    db.create_session(SESSION_ID, source="discord", model="model-a")
    db.update_system_prompt(SESSION_ID, prompt)

    write_route(db)

    assert db.get_session(SESSION_ID)["system_prompt"] == prompt


class _CompressionDB:
    def __init__(self, prompt: str):
        self.prompt = prompt

    def get_compression_tip(self, _parent):
        return "child"

    def get_session(self, _session_id):
        return {"ended_at": None, "system_prompt": self.prompt}

    def get_messages_as_conversation(self, _session_id):
        return [{"role": "user", "content": "continued"}]


def test_compression_child_does_not_seed_a_stale_route_prompt():
    agent = SimpleNamespace(
        session_id="parent",
        model="model-b",
        provider="openrouter",
        platform="cli",
        pass_session_id=False,
        _cached_system_prompt=None,
        _memory_manager=None,
        context_compressor=SimpleNamespace(on_session_start=lambda *args, **kwargs: None),
    )

    recovered = _adopt_live_compression_child(agent, _CompressionDB(_prompt("model-a")), "parent")

    assert recovered == [{"role": "user", "content": "continued"}]
    assert agent.session_id == "child"
    assert agent._cached_system_prompt is None

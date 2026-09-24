"""Route metadata commits must not destroy a valid stored prompt snapshot."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

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
    raw = db._conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = ?", (SESSION_ID,)
    ).fetchone()
    assert raw["system_prompt"] is None
    assert raw["system_prompt_hash"] is not None
    assert db._conn.execute(
        "SELECT COUNT(*) FROM system_prompts WHERE hash = ?", (raw["system_prompt_hash"],)
    ).fetchone()[0] == 1


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
        _cached_system_prompt=_prompt("model-a"),
        _memory_manager=None,
        context_compressor=SimpleNamespace(on_session_start=lambda *args, **kwargs: None),
    )

    recovered = _adopt_live_compression_child(agent, _CompressionDB(_prompt("model-a")), "parent")

    assert recovered == [{"role": "user", "content": "continued"}]
    assert agent.session_id == "child"
    assert agent._cached_system_prompt is None


def _restore_agent(db, *, model: str, provider: str, rebuilt: str) -> MagicMock:
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = SESSION_ID
    agent.model = model
    agent.provider = provider
    agent.platform = "discord"
    agent.pass_session_id = False
    agent._session_db = db
    agent._use_prompt_caching = False
    agent._persist_disabled = True
    agent._bot_mode_protocol = False
    agent.enabled_toolsets = agent.disabled_toolsets = None
    agent.tools = []
    agent._build_system_prompt = MagicMock(return_value=rebuilt)
    return agent


def _restore_next_turn(db, *, model: str, provider: str, rebuilt: str, caplog):
    from agent.conversation_loop import _restore_or_build_system_prompt

    agent = _restore_agent(db, model=model, provider=provider, rebuilt=rebuilt)
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    return agent, warnings


def test_same_route_commit_reuses_snapshot_without_warning(db, caplog):
    prompt = _prompt("model-a", "openrouter")
    db.create_session(SESSION_ID, source="discord", model="model-a")
    db.update_system_prompt(SESSION_ID, prompt)
    db.update_session_model(
        SESSION_ID, "model-a", provider="openrouter", base_url="https://example.test/v1"
    )

    agent, warnings = _restore_next_turn(
        db, model="model-a", provider="openrouter", rebuilt="unexpected rebuild", caplog=caplog
    )

    assert agent._cached_system_prompt == prompt
    agent._build_system_prompt.assert_not_called()
    assert warnings == []


def test_real_route_change_rebuilds_and_persists_without_warning(db, caplog):
    old_prompt = _prompt("model-a", "openrouter")
    new_prompt = _prompt("model-b", "openrouter")
    db.create_session(SESSION_ID, source="discord", model="model-a")
    db.update_system_prompt(SESSION_ID, old_prompt)
    db.update_session_model(
        SESSION_ID, "model-b", provider="openrouter", base_url="https://example.test/v1"
    )

    agent, warnings = _restore_next_turn(
        db, model="model-b", provider="openrouter", rebuilt=new_prompt, caplog=caplog
    )

    agent._build_system_prompt.assert_called_once()
    assert agent._cached_system_prompt == new_prompt
    assert db.get_session(SESSION_ID)["system_prompt"] == new_prompt
    assert warnings == []
    assert any(
        record.levelno == logging.INFO and "stale runtime identity" in record.getMessage()
        for record in caplog.records
    )


def test_genuinely_missing_snapshot_keeps_the_warning(db, caplog):
    db.create_session(SESSION_ID, source="discord", model="model-a")
    db.update_system_prompt(SESSION_ID, _prompt())
    db.update_system_prompt(SESSION_ID, None)

    agent, warnings = _restore_next_turn(
        db, model="model-a", provider="openrouter", rebuilt="rebuilt", caplog=caplog
    )

    assert agent._cached_system_prompt == "rebuilt"
    assert any(
        "is null; rebuilding" in warning.getMessage()
        and "update_system_prompt write path" in warning.getMessage()
        for warning in warnings
    )

"""Route metadata commits must not destroy a valid stored prompt snapshot."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


def _render_route_prompt(model: str, provider: str) -> str:
    """Render the real modern prompt shape without depending on external files."""
    from agent.system_prompt import build_system_prompt_parts

    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=["terminal"],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _execution_guidance="auto",
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model=model,
        provider=provider,
        platform="discord",
        pass_session_id=False,
        session_id=SESSION_ID,
        _emit_status=lambda *_args, **_kwargs: None,
    )
    with (
        patch("agent.prompt_builder.load_soul_md", return_value=""),
        patch("agent.prompt_builder.build_environment_hints", return_value="Host: test"),
        patch("agent.prompt_builder.build_context_files_prompt", return_value=""),
    ):
        parts = build_system_prompt_parts(agent)
    return "\n\n".join(parts.values())


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
        _cached_system_prompt=None,
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



@pytest.mark.asyncio
async def test_gateway_model_selection_rebuilds_renderer_prompt_and_replaces_blob(db, caplog, tmp_path):
    """Exercise the production /model persistence boundary and content-addressed prompt store.

    The gateway can reach model="" / provider="" before the request
    (tests/gateway/test_empty_model_fallback.py and production issue #35314).
    The first turn persists its renderer-owned prompt. A later /model commit preserves that blob,
    evicts the cached agent, and the fresh agent must reject the incomplete modern identity,
    rebuild model-gated guidance, persist the new blob, and collect the orphaned old one.
    """
    from gateway.slash_commands_model import GatewayModelCommandsMixin, _ModelSwitchContext
    from hermes_state import AsyncSessionDB

    selected_model = "deepseek/deepseek-v4-flash"
    stored = _render_route_prompt("", "")
    rebuilt = _render_route_prompt(selected_model, "openrouter")

    assert "\nModel:" not in stored
    assert "\nProvider:" not in stored
    assert "Execution discipline" not in stored
    assert "Execution discipline" in rebuilt

    db.create_session(SESSION_ID, source="discord", model="", system_prompt=stored)
    raw_before = db._conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = ?", (SESSION_ID,)
    ).fetchone()
    old_hash = raw_before["system_prompt_hash"]
    assert raw_before["system_prompt"] is None
    assert old_hash is not None
    assert db.get_session(SESSION_ID)["system_prompt"] == stored
    assert db._conn.execute(
        "SELECT COUNT(*) FROM system_prompts WHERE hash = ?", (old_hash,)
    ).fetchone()[0] == 1

    source = SimpleNamespace(platform="discord")
    session_key = "agent:main:discord:dm:route-prompt"
    session_entry = SimpleNamespace(session_id=SESSION_ID, was_auto_reset=False)
    store = SimpleNamespace(
        get_or_create_session=AsyncMock(return_value=session_entry),
        set_model_override=AsyncMock(),
    )
    runner = SimpleNamespace(
        _session_db=AsyncSessionDB(db),
        async_session_store=store,
        _pending_model_notes={},
        _session_model_overrides={},
        _pending_one_turn_model_restores={},
        _evict_cached_agent=MagicMock(),
    )
    ctx = _ModelSwitchContext(
        session_key=session_key,
        source=source,
        config_path=tmp_path / "config.yaml",
        persist_global=False,
        current_model="",
        current_provider="",
    )
    result = SimpleNamespace(
        new_model=selected_model,
        target_provider="openrouter",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        provider_label="OpenRouter",
        request_overrides={},
        runtime_capabilities={},
    )

    await GatewayModelCommandsMixin._record_model_switch(
        runner, result, ctx, source=source, one_turn=False, picker=False
    )

    store.get_or_create_session.assert_awaited_once_with(source)
    store.set_model_override.assert_awaited_once()
    runner._evict_cached_agent.assert_called_once_with(session_key)

    # /model owns route metadata, not prompt bytes: the content-addressed snapshot survives
    # until the fresh agent decides whether those bytes match its runtime.
    raw_after_switch = db._conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = ?", (SESSION_ID,)
    ).fetchone()
    assert raw_after_switch["system_prompt"] is None
    assert raw_after_switch["system_prompt_hash"] == old_hash
    assert db.get_session(SESSION_ID)["system_prompt"] == stored

    agent, warnings = _restore_next_turn(
        db,
        model=selected_model,
        provider="openrouter",
        rebuilt=rebuilt,
        caplog=caplog,
    )

    agent._build_system_prompt.assert_called_once()
    assert agent._cached_system_prompt == rebuilt
    assert warnings == []

    # Rebuild replaces the session's blob reference and update_system_prompt() remains the GC owner:
    # the pre-route blob is gone, the rebuilt model-aware blob is the only live prompt object.
    raw_after_restore = db._conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = ?", (SESSION_ID,)
    ).fetchone()
    assert raw_after_restore["system_prompt"] is None
    assert raw_after_restore["system_prompt_hash"] not in (None, old_hash)
    assert db.get_session(SESSION_ID)["system_prompt"] == rebuilt
    assert db._conn.execute(
        "SELECT COUNT(*) FROM system_prompts WHERE hash = ?", (old_hash,)
    ).fetchone()[0] == 0

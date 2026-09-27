"""Regression for #80646: the ``agent_context`` handed to memory providers follows the platform
(``cron`` / ``subagent`` skip writes per the ``MemoryProvider.initialize`` contract) instead of a
hardcoded ``"primary"`` that let cron turns land in stores configured to skip them.
"""

from types import SimpleNamespace

import pytest

from agent.agent_init import (
    _GATEWAY_IDENTITY_PARAMS,
    _memory_provider_init_kwargs,
    _write_origin_for_platform,
)


def _fake_agent():
    """The attribute surface ``_memory_provider_init_kwargs`` reads."""
    return SimpleNamespace(
        session_id="sess-80646", _session_db=None, _emit_warning=None, _emit_status=None,
        session_cwd=None, **{f"_{name}": None for name in _GATEWAY_IDENTITY_PARAMS},
    )


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("cron", "cron"), ("subagent", "subagent"), ("telegram", "primary"), (None, "primary")],
)
def test_agent_context_follows_the_platform(platform, expected):
    assert _memory_provider_init_kwargs(_fake_agent(), platform)["agent_context"] == expected


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("cron", "cron"), ("subagent", "subagent"), ("cli", "assistant_tool")],
)
def test_initialized_agent_keeps_runtime_write_origin(platform, expected, tmp_path, monkeypatch):
    """The real constructor must preserve provenance past _SESSION_STATE defaults."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    agent = AIAgent(
        model="test-model",
        provider="custom",
        base_url="http://127.0.0.1:9/v1",
        api_key="test-key",
        platform=platform,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    try:
        assert agent._memory_write_origin == expected
    finally:
        agent.close()


def test_cron_session_disables_supermemory_writes(tmp_path, monkeypatch):
    """Through the real bundled provider: the scheduler's kwargs must switch writes off,
    an interactive session's must leave them on (empty hermes_home → config defaults)."""
    from plugins.memory.supermemory import SupermemoryMemoryProvider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("SUPERMEMORY_API_KEY", raising=False)
    by_platform = {}
    for platform in ("cron", "cli"):
        provider = SupermemoryMemoryProvider()
        provider.initialize(**_memory_provider_init_kwargs(_fake_agent(), platform))
        by_platform[platform] = provider._write_enabled
    assert by_platform == {"cron": False, "cli": True}


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("cron", "cron"),
        ("subagent", "subagent"),
        ("telegram", "assistant_tool"),
        (None, "assistant_tool"),
    ],
)
def test_skill_write_origin_follows_autonomous_runtime_identity(platform, expected):
    """Cron/delegation reuse the same canonical platform identity as memory scoping."""
    assert _write_origin_for_platform(platform) == expected

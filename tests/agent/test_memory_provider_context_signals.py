"""Core memory-provider activation keeps automatic writes separate from explicit tools."""

from __future__ import annotations

import json
from unittest.mock import patch


class ExternalMemoryService:
    """A service-boundary provider that consumes the additive core capability signals."""

    name = "external-memory-service"

    def __init__(self):
        self.init_kwargs = None
        self.auto_sync_calls = []
        self.service_ready = False

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.init_kwargs = dict(kwargs)
        self.auto_sync = kwargs["auto_sync"]
        self.tools_available = kwargs["tools_available"]
        self.service_ready = bool(self.tools_available)

    def get_tool_schemas(self):
        return [{
            "name": "mnemosyne_recall",
            "description": "Recall a stored fact",
            "parameters": {"type": "object", "properties": {}},
        }]

    def handle_tool_call(self, tool_name, args, **kwargs):
        if not self.service_ready or tool_name != "mnemosyne_recall":
            raise RuntimeError("memory service is unavailable")
        return json.dumps({"available": True})

    def sync_turn(self, user_content, assistant_content, *, session_id="", **kwargs):
        if self.auto_sync:
            self.auto_sync_calls.append((user_content, assistant_content, session_id))

    def shutdown(self):
        self.service_ready = False

    def on_session_end(self, messages):
        pass


class LegacyContextProvider:
    """A pre-split provider that continues to use only the old agent_context contract."""

    name = "legacy-context-provider"

    def __init__(self):
        self.agent_context = None
        self.service_ready = False

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.agent_context = kwargs.get("agent_context", "primary")
        # Preserve the old provider's existing skip_contexts behavior unchanged.
        self.service_ready = self.agent_context not in {"cron", "subagent", "flush"}

    def get_tool_schemas(self):
        return []

    def shutdown(self):
        pass

    def on_session_end(self, messages):
        pass


def _agent_with_provider(provider, *, platform, enabled_toolsets, disabled_toolsets=None):
    config = {"memory": {"provider": "external-memory-service"}, "agent": {}}
    with (
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        from run_agent import AIAgent

        return AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id=f"session-{platform}",
            platform=platform,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets or [],
        )


def test_cron_disables_auto_sync_without_hiding_explicit_provider_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    provider = ExternalMemoryService()
    agent = _agent_with_provider(provider, platform="cron", enabled_toolsets=["memory"])
    try:
        assert agent._memory_manager is not None
        assert provider.init_kwargs["agent_context"] == "cron"
        assert provider.init_kwargs["auto_sync"] is False
        assert provider.init_kwargs["tools_available"] is True
        assert "mnemosyne_recall" in agent.valid_tool_names
        assert any(
            tool.get("function", {}).get("name") == "mnemosyne_recall"
            for tool in agent.tools
        )
        result = json.loads(agent._memory_manager.handle_tool_call("mnemosyne_recall", {}))
        assert result == {"available": True}

        agent._memory_manager.sync_all(
            "scheduled-job-preamble", "cron result", session_id=agent.session_id,
        )
        assert agent._memory_manager.flush_pending(timeout=2.0)
        assert provider.auto_sync_calls == []
    finally:
        agent.close()


def test_disabled_memory_toolset_is_reported_separately_from_auto_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    provider = ExternalMemoryService()
    agent = _agent_with_provider(
        provider, platform="cron", enabled_toolsets=["memory"], disabled_toolsets=["memory"],
    )
    try:
        assert agent._memory_manager is not None
        assert provider.init_kwargs["auto_sync"] is False
        assert provider.init_kwargs["tools_available"] is False
        assert "mnemosyne_recall" not in agent.valid_tool_names
        assert all(
            tool.get("function", {}).get("name") != "mnemosyne_recall"
            for tool in agent.tools
        )
    finally:
        agent.close()


def test_legacy_provider_keeps_its_agent_context_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    provider = LegacyContextProvider()
    agent = _agent_with_provider(provider, platform="cron", enabled_toolsets=["memory"])
    try:
        assert provider.agent_context == "cron"
        assert provider.service_ready is False
    finally:
        agent.close()

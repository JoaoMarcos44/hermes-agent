"""Regression for #111380 — cron toolset resolution must fail closed, not open."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cron.scheduler import (
    _CronToolsetResolutionBlocked,
    _cron_toolset_resolution_failure_mode,
    _resolve_cron_enabled_toolsets,
    _resolve_cron_agent_setup,
    run_job,
)


class TestCronToolsetResolutionFailureMode:
    def test_default_is_deny(self):
        assert _cron_toolset_resolution_failure_mode({}) == "deny"
        assert _cron_toolset_resolution_failure_mode({"cron": {}}) == "deny"
        assert _cron_toolset_resolution_failure_mode({"cron": {"toolset_resolution_failure": ""}}) == "deny"

    def test_explicit_values(self):
        for deny_alias in ("deny", "DENY", "fail_closed", "block", "empty", "closed"):
            assert _cron_toolset_resolution_failure_mode(
                {"cron": {"toolset_resolution_failure": deny_alias}}) == "deny"
        for full_alias in ("full", "FULL", "allow", "fail_open", "open", "all"):
            assert _cron_toolset_resolution_failure_mode(
                {"cron": {"toolset_resolution_failure": full_alias}}) == "full"

    def test_unknown_defaults_to_deny_with_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            mode = _cron_toolset_resolution_failure_mode(
                {"cron": {"toolset_resolution_failure": "weird"}})
        assert mode == "deny"
        assert any("Unknown cron.toolset_resolution_failure" in r.message for r in caplog.records)


class TestResolveCronEnabledToolsetsFailClosed:
    def test_platform_failure_deny_raises_blocked(self):
        job = {"id": "j1"}
        cfg = {}
        with patch("hermes_cli.tools_config._get_platform_tools",
                   side_effect=RuntimeError("simulated failure")):
            with pytest.raises(_CronToolsetResolutionBlocked) as exc:
                _resolve_cron_enabled_toolsets(job, cfg)
            assert "fail-closed" in str(exc.value)

    def test_platform_failure_full_returns_none(self):
        job = {"id": "j1"}
        cfg = {"cron": {"toolset_resolution_failure": "full"}}
        with patch("hermes_cli.tools_config._get_platform_tools",
                   side_effect=RuntimeError("boom")):
            result = _resolve_cron_enabled_toolsets(job, cfg)
            assert result is None

    def test_platform_failure_unknown_defaults_to_deny(self):
        job = {"id": "j1"}
        cfg = {"cron": {"toolset_resolution_failure": "bogus"}}
        with patch("hermes_cli.tools_config._get_platform_tools",
                   side_effect=RuntimeError("boom")):
            with pytest.raises(_CronToolsetResolutionBlocked):
                _resolve_cron_enabled_toolsets(job, cfg)

    def test_per_job_mcp_merge_failure_deny(self):
        job = {"id": "j1", "enabled_toolsets": ["web"]}
        cfg = {}
        with patch("hermes_cli.tools_config.enabled_mcp_server_names",
                   side_effect=RuntimeError("mcp boom")):
            with pytest.raises(_CronToolsetResolutionBlocked):
                _resolve_cron_enabled_toolsets(job, cfg)

    def test_per_job_mcp_merge_failure_full(self):
        job = {"id": "j1", "enabled_toolsets": ["web"]}
        cfg = {"cron": {"toolset_resolution_failure": "full"}}
        with patch("hermes_cli.tools_config.enabled_mcp_server_names",
                   side_effect=RuntimeError("mcp boom")):
            result = _resolve_cron_enabled_toolsets(job, cfg)
            assert result is None

    def test_per_job_bypasses_platform_failure(self):
        job = {"id": "j1", "enabled_toolsets": ["web", "terminal"]}
        cfg = {}
        with patch("hermes_cli.tools_config._get_platform_tools",
                   side_effect=RuntimeError("should not be called")):
            with patch("hermes_cli.tools_config.enabled_mcp_server_names", return_value=set()):
                result = _resolve_cron_enabled_toolsets(job, cfg)
                assert result == ["web", "terminal"]

    def test_no_per_job_uses_platform_result(self):
        job = {"id": "j1"}
        cfg = {}
        with patch("hermes_cli.tools_config._get_platform_tools",
                   return_value={"web", "memory"}):
            result = _resolve_cron_enabled_toolsets(job, cfg)
            assert result == ["memory", "web"]


class TestCronAgentSetupBlocksOnToolsetFailure:
    def test_setup_blocked_on_platform_failure_deny(self):
        job = {"id": "j1", "name": "n1"}

        class FakeJC:
            cfg = {}
            model = "test-model"
            model_cfg = {}
            cron_default_provider = ""

        jc = FakeJC()
        with patch("hermes_cli.tools_config._get_platform_tools",
                   side_effect=RuntimeError("boom")), \
             patch("cron.scheduler._guard_job_credential_exfil"), \
             patch("cron.scheduler._preflight_or_block", return_value=None):
            setup = _resolve_cron_agent_setup(job, "j1", "n1", jc)
            assert setup.blocked is not None
            assert setup.blocked[0] is False
            assert "[blocked_config]" in setup.blocked[3]
            assert "fail-closed" in setup.blocked[3]

    def test_setup_not_blocked_when_full(self):
        job = {"id": "j1", "name": "n1"}

        class FakeJC:
            cfg = {"cron": {"toolset_resolution_failure": "full"}}
            model = "test-model"
            model_cfg = {}
            cron_default_provider = ""

        jc = FakeJC()
        with patch("hermes_cli.tools_config._get_platform_tools",
                   side_effect=RuntimeError("boom")), \
             patch("cron.scheduler._guard_job_credential_exfil"), \
             patch("cron.scheduler._preflight_or_block", return_value=None), \
             patch("cron.scheduler._resolve_job_runtime",
                   return_value=({"api_key": "k", "base_url": "u", "provider": "p", "api_mode": "chat_completions"}, "m")), \
             patch("cron.scheduler._resolve_job_reasoning_config", return_value=None), \
             patch("cron.scheduler.get_fallback_chain", return_value=[]), \
             patch("cron.scheduler._load_credential_pool", return_value=None), \
             patch("cron.scheduler._init_cron_mcp_tools"), \
             patch("cron.scheduler._cron_preflight_enabled", return_value=False):
            setup = _resolve_cron_agent_setup(job, "j1", "n1", jc)
            assert setup.blocked is None
            assert setup.enabled_toolsets is None


class TestRunJobFailClosed:
    def test_run_job_blocked_before_agent_construction(self, tmp_path):
        job = {"id": "deny-job", "name": "deny test", "prompt": "do thing"}
        fake_db = MagicMock()
        fake_db.get_compression_tip.side_effect = lambda x: x

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=fake_db), \
             patch("cron.scheduler._guard_job_credential_exfil"), \
             patch("cron.scheduler._preflight_or_block", return_value=None), \
             patch("hermes_cli.tools_config._get_platform_tools", side_effect=RuntimeError("boom")), \
             patch("run_agent.AIAgent") as mock_agent_cls:

            fake_jc = MagicMock()
            fake_jc.cfg = {}
            fake_jc.model = "test-model"
            fake_jc.model_cfg = {}
            fake_jc.cron_default_provider = ""
            with patch("cron.scheduler._load_cron_job_config", return_value=fake_jc):
                success, output, final, error = run_job(job)
                assert success is False
                assert "[blocked_config]" in (error or "")
                assert "BLOCKED" in output
                assert "fail-closed" in (error or "")
                assert not mock_agent_cls.called

    def test_run_job_full_fallback_still_runs_agent(self, tmp_path):
        job = {"id": "full-job", "name": "full test", "prompt": "do thing"}
        fake_db = MagicMock()
        fake_db.get_compression_tip.side_effect = lambda x: x

        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=fake_db), \
             patch("hermes_cli.tools_config._get_platform_tools", side_effect=RuntimeError("boom")), \
             patch("run_agent.AIAgent") as mock_agent_cls:

            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            fake_jc = MagicMock()
            fake_jc.cfg = {"cron": {"toolset_resolution_failure": "full"}}
            fake_jc.model = "test-model"
            fake_jc.model_cfg = {}
            fake_jc.cron_default_provider = ""
            with patch("cron.scheduler._load_cron_job_config", return_value=fake_jc), \
                 patch("cron.scheduler._guard_job_credential_exfil"), \
                 patch("cron.scheduler._preflight_or_block", return_value=None), \
                 patch("cron.scheduler._resolve_job_runtime",
                       return_value=({"api_key": "k", "base_url": "u", "provider": "p", "api_mode": "chat_completions", "requested_provider": None}, "test-model")), \
                 patch("cron.scheduler._resolve_job_reasoning_config", return_value=None), \
                 patch("cron.scheduler.get_fallback_chain", return_value=[]), \
                 patch("cron.scheduler._load_credential_pool", return_value=None), \
                 patch("cron.scheduler._init_cron_mcp_tools"), \
                 patch("cron.scheduler._cron_preflight_enabled", return_value=False):

                setup = _resolve_cron_agent_setup(job, "full-job", "full test", fake_jc)
                assert setup.blocked is None
                assert setup.enabled_toolsets is None


class TestConfigDefault:
    def test_default_config_has_deny(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["cron"]["toolset_resolution_failure"] == "deny"

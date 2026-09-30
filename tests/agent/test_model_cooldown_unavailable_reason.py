"""Model-scoped credential cooldowns must not masquerade as missing credentials (#128995).

The production pool intentionally keeps a credential healthy when only one model is benched.
These regressions exercise the persisted pool plus both shared unavailable-client raise paths.
"""
import json
import time
import types

import pytest

PROVIDER = "openai-codex"
MODEL = "gpt-5.3-codex"
OTHER_MODEL = "gpt-5.3-codex-mini"


@pytest.fixture
def model_benched_codex(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]

    reset_at = time.time() + 2 * 3600
    (root / "auth.json").write_text(json.dumps({"credential_pool": {PROVIDER: [{
        "id": "cred-0", "label": "acct", "auth_type": "oauth", "priority": 0,
        "source": "manual", "access_token": "tok-account-a", "refresh_token": "rt-0",
        "expires_at_ms": 4_000_000_000_000, "model_cooldowns": {MODEL: reset_at},
    }]}}))

    from agent.credential_pool import load_pool
    pool = load_pool(PROVIDER)
    assert pool.has_credentials()
    assert pool.has_available(model=MODEL) is False
    assert pool.has_available(model=OTHER_MODEL) is True
    return pool


def test_shared_missing_credentials_message_names_the_model_cooldown(model_benched_codex):
    from agent.auxiliary_unavailable import (
        missing_provider_credentials_message, pool_cooldown_message)

    message = missing_provider_credentials_message(PROVIDER, model=MODEL)

    assert f"cooling down for model '{MODEL}'" in message
    assert "no credentials were found" not in message
    assert "no API key was found" not in message
    assert f"`hermes auth reset {PROVIDER}`" in message
    # Negative controls: another model is usable, and the legacy model-less helper
    # keeps its credential-wide-only contract.
    assert pool_cooldown_message(PROVIDER, model=OTHER_MODEL) is None
    assert pool_cooldown_message(PROVIDER) is None


def test_agent_init_raises_cooldown_not_setup_error(model_benched_codex):
    from agent.agent_init import _routed_client_kwargs
    from agent.auxiliary_unavailable import ProviderCredentialsExhaustedError

    agent = types.SimpleNamespace(provider=PROVIDER, model=MODEL)
    with pytest.raises(ProviderCredentialsExhaustedError) as exc_info:
        _routed_client_kwargs(agent, fallback_model=None, _provider_timeout=None)

    message = str(exc_info.value)
    assert f"cooling down for model '{MODEL}'" in message
    assert "no credentials were found" not in message


def test_auxiliary_route_names_model_cooldown(model_benched_codex):
    from agent.auxiliary_client import _resolve_call_client
    from agent.auxiliary_unavailable import AuxiliaryClientUnavailable

    with pytest.raises(AuxiliaryClientUnavailable) as exc_info:
        _resolve_call_client(
            "approval", provider=PROVIDER, model=MODEL, base_url=None, api_key=None,
            resolved_provider=PROVIDER, resolved_model=MODEL, resolved_base_url=None,
            resolved_api_key=None, resolved_api_mode=None, main_runtime=None, async_mode=False,
        )

    message = str(exc_info.value)
    assert f"cooling down for model '{MODEL}'" in message
    assert "no credentials were found" not in message

"""Shared custom endpoints must never determine credential-pool ownership by URL alone."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import credential_pool as cp
from hermes_cli import runtime_provider as rp
from hermes_cli.runtime_provider_backends import _resolve_openrouter_runtime
from hermes_cli.runtime_provider_custom import _resolve_direct_alias_runtime


ENDPOINT = "https://openrouter.ai/api/v1"
MAIN_KEY = "main-key-12345678"
SECOND_KEY = "second-key-12345678"


def _same_url_provider():
    return [(
        "second",
        {
            "name": "Second", "provider_key": "second",
            "base_url": ENDPOINT, "api_key": SECOND_KEY,
        },
    )]


def _sibling_pool():
    entry = SimpleNamespace(
        runtime_api_key=SECOND_KEY, access_token=SECOND_KEY,
        runtime_base_url=ENDPOINT, base_url=ENDPOINT,
    )
    pool = MagicMock()
    pool.provider = "second"
    pool.has_credentials.return_value = True
    pool.select.return_value = entry
    return pool


def test_bare_custom_owner_key_rejects_same_url_sibling_pool():
    with patch.object(cp, "_iter_custom_providers", return_value=_same_url_provider()):
        assert cp.custom_provider_pool_key_candidates_for_owner(
            ENDPOINT, provider_name="custom", api_key=MAIN_KEY
        ) == []
        assert cp.resolve_runtime_pool_key(
            "custom", ENDPOINT, requested_provider="custom", api_key=MAIN_KEY
        ) == "custom"
        assert not cp.credential_pool_matches_provider(
            "second", "custom", base_url=ENDPOINT,
            requested_provider="custom", api_key=MAIN_KEY,
        )


def test_exact_key_can_select_its_same_url_named_pool():
    with patch.object(cp, "_iter_custom_providers", return_value=_same_url_provider()):
        assert cp.custom_provider_pool_key_candidates_for_owner(
            ENDPOINT, provider_name="custom", api_key=SECOND_KEY
        ) == ["second", "custom:second"]
        assert cp.credential_pool_matches_provider(
            "second", "custom", base_url=ENDPOINT,
            requested_provider="custom", api_key=SECOND_KEY,
        )


def test_direct_alias_explicit_key_beats_same_url_sibling_pool(monkeypatch):
    sibling = _sibling_pool()
    monkeypatch.setattr(cp, "_iter_custom_providers", lambda: iter(_same_url_provider()))
    monkeypatch.setattr(rp, "load_pool", lambda _key: sibling)
    monkeypatch.setattr(rp, "_get_model_config", lambda: {})
    monkeypatch.setattr(rp, "_host_gated_env_key_candidates", lambda *_a, **_kw: [])

    result = _resolve_direct_alias_runtime("custom", MAIN_KEY, ENDPOINT)

    assert result["api_key"] == MAIN_KEY
    assert result["base_url"] == ENDPOINT


def test_bare_custom_openrouter_uses_its_model_key_before_sibling_pool(monkeypatch):
    sibling = _sibling_pool()
    monkeypatch.setattr(cp, "_iter_custom_providers", lambda: iter(_same_url_provider()))
    monkeypatch.setattr(rp, "load_pool", lambda _key: sibling)
    monkeypatch.setattr(
        rp, "_get_model_config",
        lambda: {"provider": "custom", "base_url": ENDPOINT, "api_key": MAIN_KEY},
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider_backends.get_secret_str",
        lambda *_a, **_kw: "",
    )

    result = _resolve_openrouter_runtime(requested_provider="custom")

    assert result["provider"] == "custom"
    assert result["base_url"] == ENDPOINT
    assert result["api_key"] == MAIN_KEY

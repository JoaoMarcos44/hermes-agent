"""MiniMax OAuth auxiliary routing regression for #125707."""

from unittest.mock import MagicMock

import pytest


def _compression_config():
    return {
        "provider": "minimax-oauth",
        "model": "MiniMax-M3",
        "base_url": "https://api.minimax.io/anthropic",
    }


def test_explicit_minimax_compression_uses_oauth_route_with_healthy_main(monkeypatch):
    from agent import auxiliary_client as aux

    token_provider = lambda: "fresh-minimax-token"
    captured = {}

    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda task: _compression_config() if task == "compression" else {},
    )
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials",
        lambda **kwargs: {
            "provider": "minimax-oauth",
            "api_key": token_provider,
            "base_url": "https://api.minimax.io/anthropic",
            "source": "oauth",
        },
    )

    def fake_build(api_key, base_url, **_kwargs):
        captured["api_key"] = api_key
        captured["base_url"] = base_url
        return MagicMock(name="anthropic-sdk")

    monkeypatch.setattr("agent.anthropic_adapter.build_anthropic_client", fake_build)

    client, model = aux.get_text_auxiliary_client(
        "compression",
        main_runtime={
            "provider": "custom",
            "model": "Qwen3-main",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "main-key",
        },
    )

    assert isinstance(client, aux.AnthropicAuxiliaryClient)
    assert model == "MiniMax-M3"
    assert captured == {
        "api_key": token_provider,
        "base_url": "https://api.minimax.io/anthropic",
    }
    assert client.api_key is token_provider
    assert client.chat.completions._is_oauth is False


def test_minimax_oauth_resolution_is_fail_soft_when_login_is_missing(monkeypatch):
    from agent import auxiliary_client as aux

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("not logged in")),
    )

    client, model = aux.resolve_provider_client("minimax-oauth", "MiniMax-M3")

    assert client is None
    assert model is None


def test_minimax_oauth_uses_registered_aux_model_when_model_is_omitted(monkeypatch):
    import model_tools  # noqa: F401 -- registers provider profiles
    import providers
    from agent import auxiliary_client as aux

    profile = providers.get_provider_profile("minimax-oauth")
    assert profile is not None and profile.default_aux_model

    token_provider = lambda: "fresh-minimax-token"
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials",
        lambda **_kwargs: {
            "provider": "minimax-oauth",
            "api_key": token_provider,
            "base_url": "https://api.minimax.io/anthropic",
            "source": "oauth",
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda *_args, **_kwargs: MagicMock(name="anthropic-sdk"),
    )

    client, model = aux.resolve_provider_client("minimax-oauth")

    assert isinstance(client, aux.AnthropicAuxiliaryClient)
    assert model == profile.default_aux_model



def test_unexpected_minimax_client_build_error_is_not_masked(monkeypatch):
    from agent import auxiliary_client as aux

    token_provider = lambda: "fresh-minimax-token"
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_minimax_oauth_runtime_credentials",
        lambda **_kwargs: {
            "provider": "minimax-oauth",
            "api_key": token_provider,
            "base_url": "https://api.minimax.io/anthropic",
            "source": "oauth",
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("builder bug")),
    )

    with pytest.raises(RuntimeError, match="builder bug"):
        aux.resolve_provider_client("minimax-oauth", "MiniMax-M3")

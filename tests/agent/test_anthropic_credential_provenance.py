"""Credential origin survives initial routing, auxiliary forwarding and route replacement."""

import json
from pathlib import Path

import httpx
import pytest

from agent import auxiliary_client as aux
from agent import secret_scope
from agent.agent_init import _init_anthropic_client
from agent.agent_runtime_helpers import _build_anthropic_client_from_runtime, _build_switched_client
from agent.anthropic_adapter import build_anthropic_client
from agent.client_lifecycle import ClientLifecycleMixin, _swap_fallback_clients
from agent.credential_pool import CredentialPool, PooledCredential, load_pool

BASE = "https://api.anthropic.com"
API = "sk-ant-future-console-synthetic"
OAUTH = "sk-ant-future-oauth-synthetic"
MODEL = "claude-sonnet-4-5"


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("agent.anthropic_adapter._claude_code_version_cache", "0.0.0")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        for suffix in ("", "_2", "_3"):
            monkeypatch.delenv(name + suffix, raising=False)
    secret_scope.set_multiplex_active(False)
    yield
    secret_scope.set_multiplex_active(False)


def pool_for(token, kind):
    entry = PooledCredential("anthropic", kind, kind, kind, 0, "manual", token, base_url=BASE)
    pool = CredentialPool("anthropic", [entry])
    assert pool.select().id == entry.id
    return pool


class Agent(ClientLifecycleMixin):
    def __init__(self, token=API, kind="api_key"):
        self.provider = "anthropic"
        self.model = MODEL
        self.api_mode = "anthropic_messages"
        self.api_key = token
        self.base_url = BASE
        self.quiet_mode = True
        self._credential_pool = pool_for(token, kind)
        self._oauth_1m_beta_disabled = False


def wire(client, token, oauth, stream=False):
    captured = []
    payload = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL,
               "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
               "usage": {"input_tokens": 1, "output_tokens": 1}}

    def respond(request):
        captured.append(request.headers)
        if stream:
            start = {"type": "message_start", "message": payload}
            events = "event: message_start\ndata: " + json.dumps(start) + "\n\n"
            events += 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=events)
        return httpx.Response(200, json=payload)

    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(respond))
    result = client.messages.create(model=MODEL, max_tokens=8,
                                    messages=[{"role": "user", "content": "hi"}], stream=stream)
    if stream:
        with result:
            assert list(result)
    else:
        assert result.content[0].text == "ok"
    headers, = captured
    assert (headers.get("authorization") == "Bearer " + token) is oauth
    assert (headers.get("x-api-key") == token) is (not oauth)
    assert ("claude-code/" in headers.get("user-agent", "")) is oauth
    assert ("claude-code-20250219" in headers.get("anthropic-beta", "")) is oauth


def test_ambiguous_credential_requires_secret_scope(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", API)
    secret_scope.set_multiplex_active(True)
    with pytest.raises(secret_scope.UnscopedSecretError):
        client = build_anthropic_client(API, BASE)
        client.close()
    scope = secret_scope.set_secret_scope({"ANTHROPIC_API_KEY": API})
    try:
        with build_anthropic_client(API, BASE) as client:
            wire(client, API, False)
    finally:
        secret_scope.reset_secret_scope(scope)


@pytest.mark.parametrize("kind,token,oauth", [("api_key", API, False), ("oauth", OAUTH, True)])
def test_initial_client_keeps_selected_pool_origin(kind, token, oauth, monkeypatch):
    agent = Agent(token, kind)
    monkeypatch.setattr(agent._credential_pool, "select", lambda **kwargs: pytest.fail("reselected credential"))
    _init_anthropic_client(agent, token, BASE, None)
    try:
        wire(agent._anthropic_client, token, oauth)
        assert agent._is_anthropic_oauth is oauth
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("start_kind,dest_kind", [("api_key", "oauth"), ("oauth", "api_key")])
def test_switch_replaces_declaration_before_request_and_restore(monkeypatch, stream, start_kind, dest_kind):
    old = API if start_kind == "api_key" else OAUTH
    new = API if dest_kind == "api_key" else OAUTH
    monkeypatch.setenv("ANTHROPIC_API_KEY" if dest_kind == "api_key" else "ANTHROPIC_TOKEN", new)
    agent = Agent(old, start_kind)
    agent._anthropic_api_key_declared = start_kind == "api_key"
    _build_switched_client(agent, "anthropic", new, BASE, "anthropic_messages", "anthropic")
    try:
        wire(agent._anthropic_client, new, dest_kind == "oauth", stream)
        request_client = agent._create_request_anthropic_client(reason="test")
        wire(request_client, new, dest_kind == "oauth", stream)
        agent._close_request_anthropic_client(request_client, reason="request_complete")
        snapshot = {"anthropic_api_key": agent._anthropic_api_key, "anthropic_base_url": BASE,
                    "anthropic_api_key_declared": agent._anthropic_api_key_declared,
                    "is_anthropic_oauth": agent._is_anthropic_oauth}
        agent._anthropic_client.close()
        _build_anthropic_client_from_runtime(agent, snapshot)
        wire(agent._anthropic_client, new, dest_kind == "oauth", stream)
        assert agent._is_anthropic_oauth is (dest_kind == "oauth")
    finally:
        agent._anthropic_client.close()
        agent._close_cached_request_anthropic_client(reason="test")


@pytest.mark.parametrize("kind,token,oauth", [("api_key", API, False), ("oauth", OAUTH, True)])
def test_aux_auto_forwards_exact_main_pool_credential(monkeypatch, kind, token, oauth):
    pool = pool_for(token, kind)
    monkeypatch.setattr(aux, "_select_pool_entry", lambda provider: pytest.fail("reselected main credential"))
    runtime = {"provider": "anthropic", "model": MODEL, "base_url": BASE,
               "api_key": token, "api_mode": "anthropic_messages", "anthropic_api_key_declared": not oauth}
    client, model = aux.resolve_provider_client("auto", main_runtime=runtime)
    try:
        assert model == MODEL
        wire(client._real_client, token, oauth)
        assert client.chat.completions._is_oauth is oauth
    finally:
        client.close()


def test_static_api_fallback_preserves_origin_when_installed():
    fallback, _ = aux.resolve_provider_client("anthropic", MODEL, explicit_api_key=API)
    agent = Agent(OAUTH, "oauth")
    try:
        wire(fallback._real_client, API, False)
        _swap_fallback_clients(agent, fallback, "anthropic", MODEL, BASE, "anthropic_messages")
        wire(agent._anthropic_client, API, False)
        assert agent._is_anthropic_oauth is False
        agent._anthropic_client.close()
    finally:
        fallback.close()


@pytest.mark.parametrize("env_name", ["ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"])
@pytest.mark.parametrize("persisted", [False, True])
def test_env_origin_survives_pool_load_aux_and_rotation(tmp_path, monkeypatch, env_name, persisted):
    oauth = env_name != "ANTHROPIC_API_KEY"
    token = OAUTH if oauth else API
    monkeypatch.setenv(env_name, token)
    monkeypatch.setenv(env_name + "_2", token + "-second")
    (tmp_path / "config.yaml").write_text("credential_pool_strategies:\n  anthropic: round_robin\n")
    if persisted:
        rows = [{"id": str(i), "label": name, "auth_type": "api_key", "source": "env:" + name,
                 "priority": i} for i, name in enumerate((env_name, env_name + "_2"))]
        (tmp_path / "auth.json").write_text(json.dumps({"credential_pool": {"anthropic": rows}}))
    pool = load_pool("anthropic")
    monkeypatch.setattr(aux, "_select_pool_entry", lambda provider: (True, pool.select()))
    seen = set()
    for _ in range(2):
        client, _ = aux.resolve_provider_client("anthropic", MODEL)
        try:
            entry = pool.current()
            seen.add(entry.source)
            wire(client._real_client, entry.runtime_api_key, oauth)
            agent = Agent()
            agent._swap_credential(entry)
            wire(agent._anthropic_client, entry.runtime_api_key, oauth)
            agent._anthropic_client.close()
            assert entry.auth_type == ("oauth" if oauth else "api_key")
        finally:
            client.close()
    assert seen == {"env:" + env_name, "env:" + env_name + "_2"}

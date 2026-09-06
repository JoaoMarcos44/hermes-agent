"""Regression coverage for raw Codex pre-stream ReadError recovery (#104303)."""

from types import SimpleNamespace

import httpx
import pytest

from agent import relay_llm
from agent.codex_runtime import run_codex_stream


class _Stream:
    def __init__(self, events=None, error=None):
        self._events = list(events or [])
        self._error = error
        self.closed = False

    def __iter__(self):
        if self._error is not None:
            raise self._error
        return iter(self._events)

    def close(self):
        self.closed = True


def _completed_stream():
    item = SimpleNamespace(
        type="message",
        status="completed",
        content=[SimpleNamespace(type="output_text", text="Recovered.")],
    )
    return _Stream(
        [
            SimpleNamespace(type="response.output_item.done", item=item),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(status="completed", id="raw-read-retry"),
            ),
        ]
    )


def _agent(aborts):
    return SimpleNamespace(
        model="gpt-5-codex",
        provider="openai-codex",
        session_id="",
        is_subagent=False,
        _fallback_index=0,
        _interrupt_requested=False,
        _touch_activity=lambda _description: None,
        _client_log_context=lambda: "",
        _abort_request_openai_client=lambda _client, *, reason: aborts.append(reason),
    )


def _request():
    return {
        "model": "gpt-5-codex",
        "instructions": "You are Hermes.",
        "input": [{"role": "user", "content": "Ping"}],
        "tools": None,
        "store": False,
    }


def test_raw_prestream_read_error_retries_once_with_request_abort(monkeypatch):
    request = httpx.Request(
        "POST",
        "https://chatgpt.com/backend-api/codex/responses",
        content=b'{"model":"gpt-5-codex"}',
    )
    calls = {"count": 0}
    aborts = []
    agent = _agent(aborts)

    def create(**_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ReadError("receive failed", request=request)
        return _completed_stream()

    client = SimpleNamespace(responses=SimpleNamespace(create=create))

    def relay_stream(api_kwargs, opener, **_kwargs):
        return opener(api_kwargs)

    monkeypatch.setattr(relay_llm, "stream", relay_stream)

    response = run_codex_stream(agent, _request(), client=client)

    assert calls["count"] == 2
    assert response.status == "completed"
    assert response.id == "raw-read-retry"
    assert aborts == ["codex_prestream_transport_retry"]


def test_raw_read_error_after_stream_open_is_not_retried(monkeypatch):
    request = httpx.Request(
        "POST",
        "https://chatgpt.com/backend-api/codex/responses",
        content=b'{"model":"gpt-5-codex"}',
    )
    calls = {"count": 0}
    aborts = []
    agent = _agent(aborts)

    def create(**_kwargs):
        calls["count"] += 1
        return _Stream(error=httpx.ReadError("mid-stream receive failed", request=request))

    client = SimpleNamespace(responses=SimpleNamespace(create=create))

    def relay_stream(api_kwargs, opener, **kwargs):
        stream = opener(api_kwargs)
        kwargs["on_stream_created"](stream)
        return stream

    monkeypatch.setattr(relay_llm, "stream", relay_stream)

    with pytest.raises(httpx.ReadError, match="mid-stream receive failed"):
        run_codex_stream(agent, _request(), client=client)

    assert calls["count"] == 1
    assert aborts == []

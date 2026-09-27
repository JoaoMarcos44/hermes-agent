"""Regression coverage for Bedrock auxiliary request shaping (#124923)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "thread_title",
        "schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
}


def _capture_anthropic_kwargs(*, is_bedrock: bool) -> dict:
    from agent.auxiliary_client import _AnthropicCompletionsAdapter

    captured = {}
    adapter = _AnthropicCompletionsAdapter(
        MagicMock(name="anthropic_client"),
        "global.anthropic.claude-sonnet-5",
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        is_bedrock=is_bedrock,
    )

    def _fake_create(_client, api_kwargs, **_kwargs):
        captured.update(api_kwargs)
        return SimpleNamespace()

    normalized = SimpleNamespace(
        content="ok", tool_calls=None, reasoning=None, finish_reason="stop",
    )
    with patch(
        "agent.anthropic_adapter.create_anthropic_message",
        side_effect=_fake_create,
    ), patch("agent.transports.get_transport") as mock_get_transport:
        mock_get_transport.return_value.normalize_response.return_value = normalized
        adapter.create(
            model="global.anthropic.claude-sonnet-5",
            messages=[{"role": "user", "content": "title this"}],
            max_tokens=64,
            extra_body={"response_format": _RESPONSE_FORMAT},
        )
    return captured


def test_anthropic_bedrock_omits_structured_output_format():
    kwargs = _capture_anthropic_kwargs(is_bedrock=True)

    assert "format" not in (kwargs.get("output_config") or {})
    assert "response_format" not in kwargs
    assert "response_format" not in (kwargs.get("extra_body") or {})


def test_native_anthropic_still_translates_structured_output_format():
    kwargs = _capture_anthropic_kwargs(is_bedrock=False)

    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["output_config"]["format"]["schema"] == _RESPONSE_FORMAT["json_schema"]["schema"]


def test_context_probe_uses_bedrock_safe_minimum_output(monkeypatch):
    from agent import bedrock_adapter

    class _ProbeClient:
        def __init__(self):
            self.inference_configs = []

        def converse(self, **kwargs):
            self.inference_configs.append(kwargs["inferenceConfig"])
            raise RuntimeError("prompt is too long: 1300 tokens > 1200 maximum")

    client = _ProbeClient()
    monkeypatch.setattr(bedrock_adapter, "_get_bedrock_runtime_client", lambda _region: client)
    monkeypatch.setattr(bedrock_adapter, "_BEDROCK_PROBE_TIERS", (1300,))
    monkeypatch.setattr(bedrock_adapter, "_WORDS_PER_TOKEN", 1.0)

    assert bedrock_adapter.probe_bedrock_context_length(
        "openai.gpt-5.6-sol", "us-east-1",
    ) == 1200
    assert client.inference_configs == [{"maxTokens": 16}]

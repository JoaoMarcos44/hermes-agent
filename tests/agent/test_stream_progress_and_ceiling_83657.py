"""Stream keep-alive classification + wall-clock ceiling (#83657).

The stale-stream detector documents itself as killing "connections kept alive
by SSE pings but no actual data", but it timestamped every stream event — so a
content-free frame refreshed its patience and a drip-feeding provider outlived
it.  ``stream_chunk_is_progress`` is the predicate that restores the documented
behavior; ``resolve_stream_hard_timeout`` is the one bound the provider cannot
push back (issue #83657: a single 1239s call).
"""

from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import (
    resolve_stream_hard_timeout,
    stream_chunk_is_progress,
)


def _chunk(delta=None, finish_reason=None, usage=None):
    choices = (
        [SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)]
        if delta is not None or finish_reason is not None
        else []
    )
    return SimpleNamespace(choices=choices, usage=usage, model="m")


def _delta(**fields):
    base = {
        "content": None,
        "tool_calls": None,
        "reasoning_content": None,
        "reasoning": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


class TestKeepAliveDetection:
    def test_empty_delta_is_not_progress(self):
        assert stream_chunk_is_progress(_chunk(delta=_delta())) is False

    def test_chunk_without_choices_or_usage_is_not_progress(self):
        assert stream_chunk_is_progress(_chunk()) is False

    def test_anthropic_ping_is_not_progress(self):
        assert stream_chunk_is_progress(SimpleNamespace(type="ping")) is False

    @pytest.mark.parametrize(
        "delta",
        [
            _delta(content="hi"),
            _delta(reasoning_content="thinking"),
            _delta(reasoning="thinking"),
            _delta(tool_calls=[SimpleNamespace(index=0)]),
            _delta(role="assistant"),
        ],
    )
    def test_payload_deltas_are_progress(self, delta):
        assert stream_chunk_is_progress(_chunk(delta=delta)) is True

    def test_finish_reason_is_progress(self):
        assert stream_chunk_is_progress(
            _chunk(delta=_delta(), finish_reason="stop")
        ) is True

    def test_usage_only_final_chunk_is_progress(self):
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=3)
        assert stream_chunk_is_progress(_chunk(usage=usage)) is True

    def test_vendor_field_in_model_extra_is_progress(self):
        delta = _delta()
        delta.model_extra = {"thinking_delta": "..."}
        assert stream_chunk_is_progress(_chunk(delta=delta)) is True

    def test_empty_model_extra_is_not_progress(self):
        delta = _delta()
        delta.model_extra = {"foo": None, "bar": ""}
        assert stream_chunk_is_progress(_chunk(delta=delta)) is False

    @pytest.mark.parametrize(
        "event",
        [
            SimpleNamespace(type="content_block_delta"),
            SimpleNamespace(type="message_stop"),
        ],
    )
    def test_non_ping_anthropic_events_are_progress(self, event):
        assert stream_chunk_is_progress(event) is True

    def test_opaque_chunk_counts_as_progress(self):
        """Unknown shapes must never be starved of patience."""
        assert stream_chunk_is_progress(SimpleNamespace(foo="bar")) is True

    def test_none_is_not_progress(self):
        assert stream_chunk_is_progress(None) is False


class TestHardCeiling:
    @pytest.fixture(autouse=True)
    def _no_config(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.timeouts.get_provider_max_call_timeout",
            lambda *_a, **_kw: None,
        )
        monkeypatch.delenv("HERMES_STREAM_MAX_CALL_SECONDS", raising=False)
        monkeypatch.delenv("HERMES_API_TIMEOUT", raising=False)

    def test_defaults_to_documented_api_timeout(self):
        assert resolve_stream_hard_timeout("nous", "gpt-5.6-luna-pro", 180.0) == 1800.0

    def test_derived_ceiling_never_below_the_stale_detector(self):
        """A ceiling under the idle patience would preempt the detector."""
        assert resolve_stream_hard_timeout("nous", "m", 3000.0) == 3000.0

    def test_infinite_stale_does_not_poison_the_derived_ceiling(self):
        assert resolve_stream_hard_timeout("nous", "m", float("inf")) == 1800.0

    def test_env_override_is_honored_verbatim(self, monkeypatch):
        monkeypatch.setenv("HERMES_STREAM_MAX_CALL_SECONDS", "300")
        assert resolve_stream_hard_timeout("nous", "m", 900.0) == 300.0

    def test_zero_disables_the_ceiling(self, monkeypatch):
        monkeypatch.setenv("HERMES_STREAM_MAX_CALL_SECONDS", "0")
        assert resolve_stream_hard_timeout("nous", "m", 180.0) == float("inf")

    def test_provider_config_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_STREAM_MAX_CALL_SECONDS", "300")
        monkeypatch.setattr(
            "hermes_cli.timeouts.get_provider_max_call_timeout",
            lambda *_a, **_kw: 120.0,
        )
        assert resolve_stream_hard_timeout("nous", "m", 180.0) == 120.0

    @pytest.mark.parametrize(
        "config",
        [
            {"providers": {"nous": {"max_call_seconds": 0}}},
            {
                "providers": {
                    "nous": {
                        "max_call_seconds": 120,
                        "models": {"m": {"max_call_seconds": 0}},
                    }
                }
            },
        ],
        ids=["provider", "model"],
    )
    def test_configured_zero_disables_the_ceiling(self, monkeypatch, config):
        from hermes_cli import timeouts

        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly", lambda: config
        )
        monkeypatch.setattr(
            "hermes_cli.timeouts.get_provider_max_call_timeout",
            lambda provider, model: timeouts._lookup_provider_timeout(
                provider,
                model,
                "max_call_seconds",
                "max_call_seconds",
                allow_zero=True,
            ),
        )
        monkeypatch.delenv("HERMES_STREAM_MAX_CALL_SECONDS", raising=False)
        monkeypatch.delenv("HERMES_API_TIMEOUT", raising=False)

        assert resolve_stream_hard_timeout("nous", "m", 180.0) == float("inf")

    def test_config_lookup_failure_falls_back_to_default(self, monkeypatch):
        def boom(*_a, **_kw):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(
            "hermes_cli.timeouts.get_provider_max_call_timeout", boom
        )
        assert resolve_stream_hard_timeout("nous", "m", 180.0) == 1800.0

    def test_local_endpoints_are_exempt_by_default(self):
        assert resolve_stream_hard_timeout(
            "ollama", "llama3", 900.0, "http://localhost:11434"
        ) == float("inf")

    def test_local_endpoints_honor_an_explicit_ceiling(self, monkeypatch):
        monkeypatch.setenv("HERMES_STREAM_MAX_CALL_SECONDS", "600")
        assert resolve_stream_hard_timeout(
            "ollama", "llama3", 900.0, "http://localhost:11434"
        ) == 600.0

    def test_cloud_endpoint_still_capped(self):
        assert resolve_stream_hard_timeout(
            "nous", "m", 180.0, "https://inference-api.nousresearch.com/v1"
        ) == 1800.0

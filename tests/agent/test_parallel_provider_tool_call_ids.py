"""Regression coverage for provider-minted parallel tool-call ids rejected on replay."""

from copy import deepcopy
from types import SimpleNamespace

from agent.message_sanitization import (
    coalesce_tool_call_id,
    normalize_parallel_provider_tool_call_history,
    normalize_parallel_provider_tool_call_ids,
    uniquify_tool_call_ids,
)
from agent.turn_tool_validation import validate_tool_calls


def _call(call_id: str, name: str = "read_file") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


class _ValidationAgent:
    valid_tool_names = {"read_file"}
    _invalid_tool_retries = 0
    _invalid_json_retries = 0
    _uniquify_tool_call_ids = staticmethod(uniquify_tool_call_ids)

    @staticmethod
    def _repair_tool_call(_name):
        return None


def test_validation_normalizes_provider_parallel_ids_before_persistence(monkeypatch):
    from hermes_cli.observability import shared_metrics_model

    monkeypatch.setattr(shared_metrics_model, "record_tool_call_quality", lambda *_args, **_kwargs: None)
    original_ids = ["chatcmpl-tool-alpha", "chatcmpl-tool-beta"]
    tool_calls = [_call(call_id) for call_id in original_ids]
    assistant_message = SimpleNamespace(tool_calls=tool_calls)

    verdict = validate_tool_calls(
        _ValidationAgent(), assistant_message, "tool_calls", messages=[],
        conversation_history=None, api_call_count=1, effective_task_id=None,
    )

    normalized = [coalesce_tool_call_id(tc) for tc in tool_calls]
    assert verdict.action == "ok"
    assert all(call_id.startswith("call_") for call_id in normalized)
    assert normalized != original_ids
    assert len(set(normalized)) == len(normalized)

    repeated = [_call(call_id) for call_id in original_ids]
    uniquify_tool_call_ids(repeated)
    normalize_parallel_provider_tool_call_ids(repeated)
    assert [coalesce_tool_call_id(tc) for tc in repeated] == normalized


def test_legacy_wire_repair_preserves_pairing_and_leaves_unaffected_batches_identical():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "chatcmpl-tool-alpha|fc_alpha",
                    "call_id": "chatcmpl-tool-alpha",
                    "response_item_id": "fc_alpha",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "id": "chatcmpl-tool-beta",
                    "call_id": "chatcmpl-tool-beta",
                    "response_item_id": "fc_beta",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "chatcmpl-tool-alpha|fc_alpha", "content": "a"},
        {"role": "tool", "tool_call_id": "chatcmpl-tool-beta", "content": "b"},
        {"role": "user", "content": "next"},
    ]

    assert normalize_parallel_provider_tool_call_history(messages)
    calls = messages[0]["tool_calls"]
    normalized = [coalesce_tool_call_id(tc) for tc in calls]
    assert all(call_id.startswith("call_") for call_id in normalized)
    assert calls[0]["id"] == f"{normalized[0]}|fc_alpha"
    assert calls[0]["response_item_id"] == "fc_alpha"
    assert calls[1]["response_item_id"] == "fc_beta"
    assert messages[1]["tool_call_id"] == f"{normalized[0]}|fc_alpha"
    assert messages[2]["tool_call_id"] == normalized[1]

    unaffected = [
        [{"role": "assistant", "tool_calls": [{"id": "chatcmpl-tool-single"}]}],
        [{
            "role": "assistant",
            "tool_calls": [{"id": "chatcmpl-tool-alpha"}, {"id": "call_beta"}],
        }],
    ]
    for case in unaffected:
        before = deepcopy(case)
        assert not normalize_parallel_provider_tool_call_history(case)
        assert case == before

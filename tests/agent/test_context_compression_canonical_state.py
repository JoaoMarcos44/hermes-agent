"""Regression coverage for #122559 canonical compression state."""

from __future__ import annotations

import copy
import json
from unittest.mock import patch

from agent.compression_marker import _COMPRESSION_MARKER_PREFIX
from agent.context_compressor import ContextCompressor


def _call(call_id: str, payload: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "/tmp/x.py", "content": payload}),
            },
        }],
    }


def _messages() -> list[dict]:
    return [
        {"role": "user", "content": "head request"},
        _call("head_call", "A" * 4_000),
        {"role": "tool", "tool_call_id": "head_call", "content": "R" * 10_000},
        {"role": "user", "content": "middle 1"},
        {"role": "assistant", "content": "middle answer 1"},
        {"role": "user", "content": "middle 2"},
        {"role": "assistant", "content": "middle answer 2"},
        {"role": "user", "content": "tail request"},
        {"role": "assistant", "content": "tail answer"},
    ]


def _compressor() -> ContextCompressor:
    return ContextCompressor(
        model="test/model",
        config_context_length=200_000,
        protect_first_n=3,
        protect_last_n=2,
        quiet_mode=True,
        tail_mode="legacy",
    )


def test_structural_noop_returns_canonical_tool_arguments() -> None:
    compressor = _compressor()
    messages = _messages()
    original_args = messages[1]["tool_calls"][0]["function"]["arguments"]

    with patch.object(compressor, "_compress_window", return_value=(3, 3)):
        result = compressor.compress(messages, current_tokens=100_000)

    assert result[1]["tool_calls"][0]["function"]["arguments"] == original_args
    assert _COMPRESSION_MARKER_PREFIX not in json.dumps(result, ensure_ascii=False)


def test_successful_compaction_carries_canonical_head_not_pruned_working_copy() -> None:
    compressor = _compressor()
    messages = _messages()
    original_args = messages[1]["tool_calls"][0]["function"]["arguments"]
    original_result = messages[2]["content"]

    with (
        patch.object(compressor, "_compress_window", return_value=(3, 7)),
        patch.object(compressor, "_generate_summary", return_value="Summary of middle turns."),
    ):
        result = compressor.compress(messages, current_tokens=100_000, force=True)

    head_call = next(
        message for message in result
        if message.get("role") == "assistant"
        and any(call.get("id") == "head_call" for call in message.get("tool_calls", []))
    )
    head_tool = next(
        message for message in result
        if message.get("role") == "tool" and message.get("tool_call_id") == "head_call"
    )
    assert head_call["tool_calls"][0]["function"]["arguments"] == original_args
    assert head_tool["content"] == original_result
    assert _COMPRESSION_MARKER_PREFIX not in head_call["tool_calls"][0]["function"]["arguments"]


def _assert_tool_pairing(messages: list[dict], *required_ids: str) -> None:
    calls = {
        call["id"]
        for message in messages
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
        if isinstance(call, dict) and call.get("id")
    }
    results = {
        message.get("tool_call_id")
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }
    assert calls == results
    for call_id in required_ids:
        assert call_id in calls


def test_tail_composes_canonical_calls_with_projected_tool_results() -> None:
    compressor = _compressor()
    messages = _messages() + [
        {"role": "user", "content": "inspect the tail"},
        _call("tail_call", "T" * 4_000),
        {"role": "tool", "tool_call_id": "tail_call", "content": "Z" * 40_000},
        {"role": "assistant", "content": "tail complete"},
    ]
    original_args = messages[10]["tool_calls"][0]["function"]["arguments"]
    projected_result = "[read_file output summarized: protected-tail pressure demotion]"

    def _project(source, *args, **kwargs):
        projected = copy.deepcopy(source)
        tail_call = next(
            message for message in projected
            if message.get("role") == "assistant"
            and any(call.get("id") == "tail_call" for call in message.get("tool_calls", []))
        )
        tail_call["tool_calls"][0]["function"]["arguments"] = json.dumps(
            {"path": "/tmp/wrong.py", "content": "MUTATED WORKING COPY"}
        )
        tail_tool = next(
            message for message in projected
            if message.get("role") == "tool" and message.get("tool_call_id") == "tail_call"
        )
        tail_tool["content"] = projected_result
        return projected, 1

    with (
        patch.object(compressor, "_compress_window", return_value=(3, 7)),
        patch.object(compressor, "_prune_old_tool_results", side_effect=_project),
        patch.object(compressor, "_generate_summary", return_value="Summary of middle turns."),
    ):
        result = compressor.compress(messages, current_tokens=100_000, force=True)

    tail_call = next(
        message for message in result
        if message.get("role") == "assistant"
        and any(call.get("id") == "tail_call" for call in message.get("tool_calls", []))
    )
    tail_tool = next(
        message for message in result
        if message.get("role") == "tool" and message.get("tool_call_id") == "tail_call"
    )
    assert tail_call["tool_calls"][0]["function"]["arguments"] == original_args
    assert tail_tool["content"] == projected_result
    _assert_tool_pairing(result, "head_call", "tail_call")


def test_second_compaction_preserves_canonical_tool_pairing() -> None:
    compressor = _compressor()
    messages = _messages()
    original_args = messages[1]["tool_calls"][0]["function"]["arguments"]

    with (
        patch.object(compressor, "_compress_window", return_value=(3, 7)),
        patch.object(compressor, "_generate_summary", return_value="First summary."),
    ):
        first = compressor.compress(messages, current_tokens=100_000, force=True)

    second_input = first + [
        {"role": "user", "content": "new middle"},
        {"role": "assistant", "content": "new middle answer"},
        {"role": "user", "content": "new tail"},
        {"role": "assistant", "content": "new tail answer"},
    ]
    with (
        patch.object(compressor, "_compress_window", return_value=(3, len(second_input) - 2)),
        patch.object(compressor, "_generate_summary", return_value="Second summary."),
    ):
        second = compressor.compress(second_input, current_tokens=100_000, force=True)

    head_call = next(
        message for message in second
        if message.get("role") == "assistant"
        and any(call.get("id") == "head_call" for call in message.get("tool_calls", []))
    )
    assert head_call["tool_calls"][0]["function"]["arguments"] == original_args
    assert _COMPRESSION_MARKER_PREFIX not in json.dumps(second, ensure_ascii=False)
    _assert_tool_pairing(second, "head_call")

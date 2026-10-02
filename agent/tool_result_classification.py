"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


REFUSAL_BLOCKED = "blocked"
REFUSAL_PENDING_APPROVAL = "pending_approval"


def _result_dict(result: Any) -> dict | None:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    try:
        data = json.loads(result.strip())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _durable_wrapped_result_dict(
    tool_name: str, result: Any, effect_disposition: str | None,
) -> dict | None:
    """Decode Hermes' untrusted wrapper only when durable metadata proves no effect."""
    if effect_disposition != "none" or not isinstance(result, str):
        return None
    opener = f'<untrusted_tool_result source="{tool_name}">\n'
    closer = "\n</untrusted_tool_result>"
    if not result.startswith(opener) or not result.endswith(closer):
        return None
    wrapped = result[len(opener):-len(closer)]
    _notice, separator, payload = wrapped.partition("\n\n")
    if not separator:
        return None
    return _result_dict(payload)


def classify_no_effect_refusal(
    tool_name: str, result: Any, *, effect_disposition: str | None = None,
) -> str | None:
    """Classify producer-owned evidence that a tool call was refused before taking effect.

    Durable effect_disposition="none" is the authority for current blocked calls. It is
    also the only license to inspect inside browser/MCP untrusted-data wrappers. Without
    that metadata, arbitrary plugin or remote-server payloads cannot forge execution state.
    The narrow legacy fallback is limited to Hermes-owned approval result shapes.
    """
    data = _result_dict(result)
    if data is None:
        data = _durable_wrapped_result_dict(tool_name, result, effect_disposition)

    if isinstance(data, dict):
        status = data.get("status")
        if tool_name == "terminal" or effect_disposition == "none":
            if status == REFUSAL_BLOCKED:
                return REFUSAL_BLOCKED
            if status in {REFUSAL_PENDING_APPROVAL, "approval_required"}:
                return REFUSAL_PENDING_APPROVAL

        error = data.get("error")
        legacy_consent_tool = tool_name in {"write_file", "patch", "execute_code"}
        if (
            (legacy_consent_tool or effect_disposition == "none")
            and isinstance(error, str)
            and "user has NOT consented" in error
            and "Do NOT retry" in error
        ):
            return REFUSAL_BLOCKED
    return None


# Set by a tool that REFUSED a call the harness judged redundant (repeated identical
# read/search). The body still carries ``"error"`` so the model reads it as a stop
# signal, but nothing failed: failure classifiers must not count it, or the cheap
# refusal feeds the streak that fires ``repeated_exact_failure_block``.
GUARDRAIL_REFUSAL_KEY = "guardrail_refusal"


def is_guardrail_refusal(result: Any) -> bool:
    """Return True when ``result`` (JSON string or parsed dict) is a harness refusal."""
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result.strip())
        except Exception:
            return False
    return isinstance(data, dict) and data.get(GUARDRAIL_REFUSAL_KEY) is True


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False

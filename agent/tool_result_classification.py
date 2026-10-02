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


def classify_no_effect_refusal(tool_name: str, result: Any) -> str | None:
    """Classify producer-owned refusal results that prove a call did not take effect.

    A tool's domain payload may legitimately contain status="blocked" after
    successful execution, so that field is execution evidence only for terminal,
    whose result envelope owns the approval status contract. Other tools require
    the explicit no-consent / do-not-retry contract emitted by write approvals.
    """
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result.strip())
        except Exception:
            data = None

    if isinstance(data, dict) and tool_name == "terminal":
        status = data.get("status")
        if status in {REFUSAL_BLOCKED, REFUSAL_PENDING_APPROVAL}:
            return status

    error = data.get("error") if isinstance(data, dict) else result
    if (
        tool_may_have_side_effect(tool_name)
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

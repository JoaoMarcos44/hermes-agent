"""Approval detection and messaging for security-policy config mutations."""

from unittest.mock import patch

import pytest

from tools.approval import check_dangerous_command, detect_dangerous_command


@pytest.mark.parametrize(
    "command",
    [
        "hermes config set approvals.single_query_mode approve",
        "hermes config set --force approvals.mode off",
        'hermes -p prod config set "security.tirith_enabled" false',
        "hermes config set command_allowlist '[\"git status\"]'",
        "hermes config unset approvals.single_query_mode",
    ],
)
def test_security_policy_config_mutations_are_dangerous(command):
    dangerous, pattern_key, description = detect_dangerous_command(command)

    assert dangerous is True
    assert pattern_key
    assert "security" in description.lower()


def test_single_query_denial_does_not_advertise_the_mutation_bypass(monkeypatch):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)

    with patch("tools.approval_context._get_single_query_approval_mode", return_value="deny"):
        result = check_dangerous_command(
            "hermes update",
            "local",
        )

    assert result["approved"] is False
    assert "single_query_mode" not in result["message"]
    assert "operator" in result["message"].lower()


def test_security_policy_approval_is_one_shot(monkeypatch):
    """An operator approval cannot be cached into a future policy mutation."""
    session_key = "security-policy-one-shot"
    decisions = iter(["always", "deny"])
    seen = []

    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", session_key)

    def operator_prompt(_command, _description, **kwargs):
        seen.append(kwargs)
        return next(decisions)

    with patch("tools.approval.prompt_dangerous_approval", side_effect=operator_prompt):
        first = check_dangerous_command(
            "hermes config set approvals.single_query_mode approve",
            "local",
        )
        second = check_dangerous_command(
            "hermes config set approvals.single_query_mode approve",
            "local",
        )

    assert first["approved"] is True
    assert second["approved"] is False
    assert len(seen) == 2
    assert seen[0]["allow_session"] is False
    assert seen[0]["allow_permanent"] is False

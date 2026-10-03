"""Auxiliary Codex response projection must preserve route-sensitive Responses semantics."""

from types import SimpleNamespace

import pytest

from agent.auxiliary_client import _CodexCompletionsAdapter


def _message(text: str, *, phase: str | None = "final_answer", status: str = "completed"):
    return SimpleNamespace(
        type="message",
        role="assistant",
        phase=phase,
        status=status,
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _reasoning(text: str):
    return SimpleNamespace(
        type="reasoning",
        id="rs_test",
        encrypted_content=None,
        summary=[SimpleNamespace(text=text)],
    )


def _final(*, status="completed", output=None, reason=None):
    return SimpleNamespace(
        status=status,
        output=output,
        output_text="",
        incomplete_details={"reason": reason} if reason else None,
        error=None,
        usage=SimpleNamespace(input_tokens=11, output_tokens=3, total_tokens=14),
    )


class _Events:
    def __init__(self, final):
        self.final = final

    def __iter__(self):
        for item in self.final.output or []:
            yield SimpleNamespace(type="response.output_item.done", item=item)
        yield SimpleNamespace(type=f"response.{self.final.status}", response=self.final)

    def close(self):
        pass


def _run(base_url: str, final, *, streamed: bool):
    class _Responses:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return _Events(final) if streamed else final

    client = SimpleNamespace(base_url=base_url, responses=_Responses())
    return _CodexCompletionsAdapter(client, "gpt-5.6-sol").create(
        messages=[{"role": "user", "content": "Summarize."}],
    )


@pytest.mark.parametrize("streamed", [False, True], ids=["object", "sse"])
@pytest.mark.parametrize(
    "base_url, final, expected_content, expected_finish, expected_tool",
    [
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(status="incomplete", output=[_message("PARTIAL")], reason="max_output_tokens"),
            "PARTIAL",
            "length",
            None,
            id="native-incomplete",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output=[_message("working", phase="commentary")]),
            None,
            "length",
            None,
            id="commentary-only",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output=[_reasoning("still thinking")]),
            None,
            "length",
            None,
            id="codex-reasoning-only",
        ),
        pytest.param(
            "https://api.x.ai/v1",
            _final(output=[_reasoning("scratch\n<response>FINAL ANSWER</response>")]),
            "FINAL ANSWER",
            "stop",
            None,
            id="xai-reasoning-answer",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output=[_message("DONE")]),
            "DONE",
            "stop",
            None,
            id="completed-final",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output=[
                SimpleNamespace(
                    type="function_call",
                    status="completed",
                    call_id="call_1",
                    name="inspect",
                    arguments='{"path":"x"}',
                )
            ]),
            None,
            "tool_calls",
            "inspect",
            id="completed-tool-call",
        ),
    ],
)
def test_auxiliary_projection_keeps_route_sensitive_completion_contract(
    streamed, base_url, final, expected_content, expected_finish, expected_tool,
):
    response = _run(base_url, final, streamed=streamed)
    choice = response.choices[0]

    assert choice.message.content == expected_content
    assert choice.finish_reason == expected_finish
    assert response.status == final.status
    assert (
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
        response.usage.total_tokens,
    ) == (11, 3, 14)
    if expected_tool is None:
        assert choice.message.tool_calls is None
    else:
        assert [call.function.name for call in choice.message.tool_calls] == [expected_tool]

"""Auxiliary Codex completion adaptation must preserve route-sensitive terminal semantics."""

from types import SimpleNamespace

import pytest

from agent.auxiliary_client import _CodexCompletionsAdapter


def _message(text, *, phase="final_answer", status="completed"):
    return SimpleNamespace(
        type="message", role="assistant", phase=phase, status=status,
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _reasoning(text):
    return SimpleNamespace(
        type="reasoning", id="rs_test", encrypted_content=None,
        summary=[SimpleNamespace(text=text)],
    )


def _final(*, status="completed", output=None, reason=None, output_text="", error=None):
    return SimpleNamespace(
        status=status, output=output, output_text=output_text,
        incomplete_details={"reason": reason} if reason else None,
        error=error,
        usage={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
    )


class _Events:
    def __init__(self, final):
        self.final = final

    def __iter__(self):
        for item in self.final.output or []:
            yield SimpleNamespace(type="response.output_item.done", item=item)
        terminal = "failed" if self.final.status == "cancelled" else self.final.status
        yield SimpleNamespace(type=f"response.{terminal}", response=self.final)

    def close(self):
        pass


def _run(base_url, final, *, streamed):
    sent = {}

    class _Responses:
        def create(self, **kwargs):
            sent.update(kwargs)
            return _Events(final) if streamed else final

    client = SimpleNamespace(base_url=base_url, responses=_Responses())
    response = _CodexCompletionsAdapter(client, "gpt-5.6-sol").create(
        messages=[{"role": "user", "content": "Summarize."}],
    )
    assert "_response_issuer_kind" not in sent
    assert "_response_issuer_model" not in sent
    return response


@pytest.mark.parametrize("streamed", [False, True], ids=["object", "sse"])
@pytest.mark.parametrize(
    "base_url, final, expected_content, expected_finish, expected_tool",
    [
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(status="incomplete", output=[_message("PARTIAL")], reason="max_output_tokens"),
            "PARTIAL", "length", None, id="native-incomplete",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output=[_message("working", phase="commentary")]),
            None, "length", None, id="commentary-only",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output=[_reasoning("still thinking")]),
            None, "length", None, id="codex-reasoning-only",
        ),
        pytest.param(
            "https://api.x.ai/v1",
            _final(output=[_reasoning("scratch\n<response>FINAL ANSWER</response>")]),
            "FINAL ANSWER", "stop", None, id="xai-reasoning-answer",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output=[_message("DONE")]),
            "DONE", "stop", None, id="completed-final",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(
                status="incomplete", reason="max_output_tokens",
                output=[SimpleNamespace(
                    type="function_call", status="completed", call_id="call_1",
                    name="inspect", arguments='{"path":"x"}',
                )],
            ),
            None, "length", "inspect", id="partial-tool-call",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output=[SimpleNamespace(
                type="function_call", status="completed", call_id="call_1",
                name="inspect", arguments='{"path":"x"}',
            )]),
            None, "tool_calls", "inspect", id="completed-tool-call",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(status="incomplete", reason="content_filter"),
            None, "content_filter", None, id="content-filter",
        ),
        pytest.param(
            "https://chatgpt.com/backend-api/codex",
            _final(output_text="DELTA FINAL"),
            "DELTA FINAL", "stop", None, id="output-text-fallback",
        ),
    ],
)
def test_route_aware_auxiliary_completion_contract(
    streamed, base_url, final, expected_content, expected_finish, expected_tool,
):
    response = _run(base_url, final, streamed=streamed)
    choice = response.choices[0]

    assert choice.message.content == expected_content
    assert choice.finish_reason == expected_finish
    assert response.status == final.status
    assert response.incomplete_details == final.incomplete_details
    assert response.error is final.error
    assert (
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
        response.usage.total_tokens,
    ) == (11, 3, 14)
    if expected_tool is None:
        assert choice.message.tool_calls is None
    else:
        assert [call.function.name for call in choice.message.tool_calls] == [expected_tool]


@pytest.mark.parametrize("streamed", [False, True], ids=["object", "sse"])
@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_route_aware_auxiliary_terminal_failures_raise(streamed, status):
    final = _final(status=status, error={"message": f"{status} upstream"})
    with pytest.raises(RuntimeError, match=f"{status} upstream"):
        _run("https://chatgpt.com/backend-api/codex", final, streamed=streamed)

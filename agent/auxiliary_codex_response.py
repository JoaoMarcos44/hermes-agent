"""Route-aware Responses-to-Chat projection for auxiliary Codex-compatible clients."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass(frozen=True)
class AuxiliaryCodexRoute:
    """Immutable wire identity shared by auxiliary request and response adaptation."""

    host: str
    wire_model: str
    is_codex_backend: bool
    is_xai_responses: bool
    is_github_responses: bool
    issuer_kind: str


def resolve_auxiliary_codex_route(client: Any, model: str) -> AuxiliaryCodexRoute:
    """Resolve the same endpoint identity used by the main Responses transport."""
    from agent.codex_responses_adapter import (
        _classify_responses_issuer,
        _wire_model_identity,
        classify_responses_route,
    )

    host = str(getattr(client, "base_url", "") or "")
    flags = classify_responses_route(SimpleNamespace(provider=None, base_url=host))
    return AuxiliaryCodexRoute(
        host=host,
        wire_model=_wire_model_identity(model),
        is_codex_backend=flags.is_codex_backend,
        is_xai_responses=flags.is_xai_responses,
        is_github_responses=flags.is_github_responses,
        issuer_kind=_classify_responses_issuer(base_url=host, **flags._asdict()),
    )


class _ResponseView:
    """Normalize output items without discarding response metadata the shared normalizer may read."""

    def __init__(self, response: Any, output: list[Any]):
        self._response = response
        self.output = output

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def _response_output_for_normalizer(final: Any) -> list[Any]:
    output = [
        SimpleNamespace(**item) if isinstance(item, dict) else item
        for item in (getattr(final, "output", None) or [])
    ]
    if output:
        return output

    status = str(getattr(final, "status", "") or "").strip().lower()
    details = getattr(final, "incomplete_details", None)
    reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
    reason = str(reason or "").strip().lower()
    output_text = getattr(final, "output_text", None)
    if status == "incomplete" and reason == "content_filter":
        return output
    if isinstance(output_text, str) and output_text.strip():
        return output

    # Historical auxiliary hosts may return an empty successful object. Keep that
    # compatibility shape, and give failed/cancelled responses an item so the shared
    # normalizer can reach its native error branch instead of failing the empty-output guard.
    return [SimpleNamespace(type="message", role="assistant", status="completed", content=[])]


def _chat_finish_reason(final: Any, native_finish_reason: str) -> str:
    status = str(getattr(final, "status", "") or "").strip().lower()
    details = getattr(final, "incomplete_details", None)
    reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
    reason = str(reason or "").strip().lower()
    if status == "incomplete" and reason == "content_filter":
        return "content_filter"
    if status == "incomplete" or native_finish_reason == "incomplete":
        return "length"
    return native_finish_reason


def _chat_usage(final: Any) -> Any:
    usage = getattr(final, "usage", None)
    if not usage:
        return None

    def value(name: str) -> int:
        raw = usage.get(name, 0) if isinstance(usage, Mapping) else getattr(usage, name, 0)
        return raw or 0

    return SimpleNamespace(
        prompt_tokens=value("input_tokens"),
        completion_tokens=value("output_tokens"),
        total_tokens=value("total_tokens"),
    )


def project_auxiliary_codex_response(
    final: Any, *, client: Any, model: str, wire_aliases: Mapping[str, str] | None = None,
) -> Any:
    """Project one Responses result into the Chat shape without erasing route-sensitive semantics."""
    from agent.codex_responses_adapter import _normalize_codex_response

    route = resolve_auxiliary_codex_route(client, model)
    view = _ResponseView(final, _response_output_for_normalizer(final))
    message, native_finish_reason = _normalize_codex_response(
        view,
        issuer_kind=route.issuer_kind,
        issuer_model=route.wire_model,
    )
    finish_reason = _chat_finish_reason(view, native_finish_reason)

    tool_calls = list(message.tool_calls or [])
    aliases = wire_aliases or {}
    for tool_call in tool_calls:
        name = tool_call.function.name
        if name in aliases:
            tool_call.function.name = aliases[name]

    content = message.content.strip() if isinstance(message.content, str) else message.content
    chat_message = SimpleNamespace(
        role="assistant",
        content=content or None,
        tool_calls=tool_calls or None,
    )
    choice = SimpleNamespace(index=0, message=chat_message, finish_reason=finish_reason)
    return SimpleNamespace(
        choices=[choice],
        model=model,
        usage=_chat_usage(final),
        status=getattr(final, "status", None),
        incomplete_details=getattr(final, "incomplete_details", None),
        error=getattr(final, "error", None),
    )

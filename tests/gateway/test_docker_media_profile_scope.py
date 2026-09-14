"""Regression tests for profile-scoped Docker MEDIA delivery (#109024)."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource

_ACTIVE_PROFILE = ContextVar("test_active_profile", default=None)


@contextmanager
def _profile_scope_for_source(source):
    token = _ACTIVE_PROFILE.set(source.profile)
    try:
        yield
    finally:
        _ACTIVE_PROFILE.reset(token)


class _MediaAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.DISCORD)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="message-1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _source():
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="D1",
        chat_type="dm",
        profile="public",
    )


def _event(source):
    return MessageEvent(text="send the report", source=source, message_id="m1")


def _capture_filter(monkeypatch, observed):
    original_filter = BasePlatformAdapter.filter_media_delivery_paths

    def capture_filter(media_files, session_key=""):
        observed.append((_ACTIVE_PROFILE.get(), session_key))
        return original_filter(media_files, session_key=session_key)

    monkeypatch.setattr(
        BasePlatformAdapter,
        "filter_media_delivery_paths",
        staticmethod(capture_filter),
    )


@pytest.mark.linux_only
def test_canonical_profile_scope_selects_profile_docker_mount(tmp_path, monkeypatch):
    """The real profile scope must select the producing profile's Docker policy."""
    from tools.environments.base import get_sandbox_dir

    hermes_home = tmp_path / "hermes"
    public_home = hermes_home / "profiles" / "public"
    public_home.mkdir(parents=True)
    public_output = tmp_path / "public-output"
    public_output.mkdir()
    public_file = public_output / "report.txt"
    public_file.write_text("public", encoding="utf-8")
    public_sandbox = tmp_path / "public-sandbox"
    (public_home / "config.yaml").write_text(
        json.dumps({
            "terminal": {
                "backend": "docker",
                "container_persistent": True,
                "docker_volumes": [f"{public_output}:/output"],
                "sandbox_dir": str(public_sandbox),
            },
        }),
        encoding="utf-8",
    )

    ambient_output = tmp_path / "ambient-output"
    ambient_output.mkdir()
    (ambient_output / "report.txt").write_text("ambient", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", json.dumps([f"{ambient_output}:/output"]))

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = lambda _source: public_home
    source = _source()

    with runner._profile_scope_for_source(source):
        assert get_sandbox_dir() == public_sandbox
        delivered = BasePlatformAdapter.filter_media_delivery_paths(
            [("/output/report.txt", False)],
            session_key="agent:public:discord:dm:D1",
        )

    assert delivered == [(str(public_file.resolve()), False)]
    assert _ACTIVE_PROFILE.get() is None


@pytest.mark.asyncio
async def test_normal_media_filter_runs_under_source_profile_scope(monkeypatch):
    """Normal adapter delivery must filter MEDIA while the routed scope is active."""
    adapter = _MediaAdapter()
    adapter.gateway_runner = SimpleNamespace(_profile_scope_for_source=_profile_scope_for_source)

    async def handler(_event):
        return "MEDIA: /root/report.png"

    adapter.set_message_handler(handler)

    observed = []
    _capture_filter(monkeypatch, observed)
    source = _source()
    await adapter._process_message_background(_event(source), "agent:public:discord:dm:D1")

    assert observed == [("public", "agent:public:discord:dm:D1")]
    assert _ACTIVE_PROFILE.get() is None


@pytest.mark.asyncio
async def test_stream_media_filter_runs_under_source_profile_scope(monkeypatch):
    """Post-stream MEDIA delivery must re-enter the source profile after the turn scope ends."""
    runner = object.__new__(GatewayRunner)
    runner._profile_scope_for_source = _profile_scope_for_source
    adapter = SimpleNamespace(
        name="test",
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        send_multiple_images=AsyncMock(),
        send_voice=AsyncMock(),
        send_video=AsyncMock(),
        send_document=AsyncMock(),
    )
    source = _source()
    session_key = "agent:public:discord:dm:D1"
    observed = []
    _capture_filter(monkeypatch, observed)

    await GatewayRunner._deliver_media_from_response(
        runner,
        "MEDIA: /root/report.png",
        _event(source),
        adapter,
        thread_metadata={},
        session_key=session_key,
    )

    assert observed == [("public", session_key)]
    assert _ACTIVE_PROFILE.get() is None


@pytest.mark.asyncio
async def test_stream_turn_forwards_session_key_to_media_delivery():
    """The streamed-turn caller must preserve the canonical session key."""
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace()
    media_delivery = AsyncMock()
    runner._adapter_for_source = lambda _source: adapter
    runner._deliver_media_from_response = media_delivery
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    source = _source()
    session_key = "agent:public:discord:dm:D1"

    result = await GatewayRunner._hmwa_deliver_turn_response(
        runner,
        _event(source),
        source,
        SimpleNamespace(session_id="session-1"),
        session_key,
        1,
        {"already_sent": True},
        [],
        "MEDIA: /root/report.png",
        None,
        False,
    )

    assert result is None
    assert media_delivery.await_args.kwargs["session_key"] == session_key


@pytest.mark.asyncio
async def test_background_media_filter_runs_under_source_profile_scope(monkeypatch):
    """Background-task MEDIA delivery must use the routed profile, not ambient state."""
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        multiplex_profiles=True,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner._profile_scope_for_source = _profile_scope_for_source
    source = _source()
    adapter = SimpleNamespace(
        extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        send=AsyncMock(),
        send_image=AsyncMock(),
        send_voice=AsyncMock(),
        send_video=AsyncMock(),
        send_image_file=AsyncMock(),
        send_document=AsyncMock(),
    )
    runner._adapter_for_source = lambda _source: adapter
    runner._thread_metadata_for_source = lambda *_args: {}
    runner._resolve_session_agent_runtime = lambda **_kwargs: ("model", {"api_key": "configured"})
    runner._resolve_turn_toolsets = lambda *_args: ([], None)
    runner._provider_routing = {}
    runner._resolve_session_reasoning_config = lambda **_kwargs: None
    runner._resolve_session_service_tier = lambda **_kwargs: None
    runner._resolve_turn_agent_config = lambda *_args: {
        "model": "model",
        "runtime": {},
        "request_overrides": None,
    }

    async def run_in_executor(_run_sync):
        return {"final_response": "MEDIA: /root/report.png", "messages": []}

    runner._run_in_executor_with_context = run_in_executor
    observed = []
    _capture_filter(monkeypatch, observed)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 1)

    await GatewayRunner._run_background_task_inner(
        runner,
        "send the report",
        source,
        "task-1",
    )

    assert observed == [("public", "agent:public:discord:dm:D1")]
    assert _ACTIVE_PROFILE.get() is None

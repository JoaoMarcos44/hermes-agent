"""API-server worker lifetime outlives request-handler cancellation (#116535).

``APIServerAdapter._run_agent()`` runs the turn on an executor worker thread while the
request handler awaits ``loop.run_in_executor``. Cancelling the handler task (client
disconnect, shutdown) runs the handler ``finally`` at once — decrementing the in-flight
count — while the worker thread keeps running and can still touch ``state.db``. The
shutdown snapshot then observes zero API runs and closes SessionDB handles under the
live worker.

The worker-owned lease (``_track_api_worker``) is incremented at worker entry and
released by the worker's own ``finally``, so the shutdown count stays positive until
the thread that can actually write has exited.
"""

import asyncio
import threading
import time
from contextlib import suppress
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run_shutdown import GatewayShutdownMixin


def _make_blocked_agent(entered: threading.Event, release: threading.Event):
    agent = MagicMock()
    agent.session_id = None
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent._last_compaction_in_place = False

    def _blocked_run_conversation(**kwargs):
        entered.set()
        assert release.wait(60.0), "worker was never released"
        return {"final_response": "done", "messages": [], "api_calls": 0, "tools": []}

    agent.run_conversation.side_effect = _blocked_run_conversation
    return agent


async def _wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_handler_cancellation_keeps_worker_counted_until_worker_exits():
    """Cancelling the handler must not release the shutdown count while blocked."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    entered = threading.Event()
    release = threading.Event()
    agent = _make_blocked_agent(entered, release)

    with patch.object(adapter, "_create_agent", return_value=agent):
        task = asyncio.create_task(adapter._run_agent(
            user_message="hello", conversation_history=[], session_id="s1"))
        # Wait until the worker is parked inside the turn itself, not just past
        # lease entry: cancelling earlier would race cold-start work before the
        # blocked region the issue describes.
        assert await _wait_for(
            lambda: adapter.active_api_worker_count() == 1 and entered.is_set()), (
            "worker never blocked inside the turn")

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        # Handler bookkeeping is gone, but the worker thread is still blocked:
        # the exact handler count reads 0 while the worker lease still holds.
        assert adapter._inflight_agent_runs == 0
        assert adapter.active_agent_work_count() == 0
        assert adapter.active_api_worker_count() == 1

        release.set()
        assert await _wait_for(lambda: adapter.active_api_worker_count() == 0), (
            "worker lease was never released")
        assert adapter.active_agent_work_count() == 0


@pytest.mark.asyncio
async def test_shutdown_snapshot_sees_worker_past_handler_cancellation():
    """The pre-teardown snapshot gates the SessionDB close on the live worker."""
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    entered = threading.Event()
    release = threading.Event()
    agent = _make_blocked_agent(entered, release)

    class _Runner:
        _api_server_hook = GatewayShutdownMixin._api_server_hook
        _active_api_run_count = GatewayShutdownMixin._active_api_run_count

    runner = _Runner()
    runner.adapters = {Platform.API_SERVER: adapter}

    with patch.object(adapter, "_create_agent", return_value=agent):
        task = asyncio.create_task(adapter._run_agent(
            user_message="hello", conversation_history=[], session_id="s1"))
        assert await _wait_for(
            lambda: adapter.active_api_worker_count() == 1 and entered.is_set()), (
            "worker never blocked inside the turn")

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        # This is the snapshot `_stop_release_runtime_state` takes before
        # `adapters.clear()`; it must stay positive while the worker is blocked.
        assert runner._active_api_run_count() >= 1

        release.set()
        assert await _wait_for(lambda: runner._active_api_run_count() == 0), (
            "shutdown count never drained after the worker exited")

"""Stale heartbeat must unblock await_child and collect a late real result.

Regression for #113222: a child that has already gone idle (or finished writing
its answer) used to leave the parent in future.result(timeout=None) forever
because the heartbeat only stopped parent activity touches.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from tools import delegate_tool
from tools.delegate_tool_dispatch import _Batch, _execute_and_aggregate


class FrozenChild:
    def __init__(self, summary=None):
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.session_id = "frozen-child"
        self.release = threading.Event()
        self.summary = summary or {"api_call_count": 1, "current_tool": None, "last_activity_ts": 1.0}

    def run_conversation(self, **_kwargs):
        self.release.wait()
        return {"final_response": "DONE", "completed": True, "messages": []}

    def get_activity_summary(self):
        return dict(self.summary)

    def hard_interrupt(self, *_args, **_kwargs):
        return True

    def close(self):
        self.release.set()


class CompletingChild:
    def __init__(self):
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.session_id = "done-child"

    def run_conversation(self, **_kwargs):
        return {"final_response": "SIBLING", "completed": True, "messages": []}

    def get_activity_summary(self):
        return {"api_call_count": 1, "current_tool": None, "last_activity_ts": 1.0}

    def hard_interrupt(self, *_args, **_kwargs):
        return True

    def close(self):
        pass


def _parent():
    return SimpleNamespace(
        session_id="parent",
        _current_task_id=None,
        _active_children=[],
        _active_children_lock=threading.Lock(),
        _touch_activity=lambda _desc: None,
        _interrupt_requested=False,
    )


def _fast_watchdog(monkeypatch, *, grace=0.05):
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(delegate_tool, "_HEARTBEAT_STALE_CYCLES_IDLE", 2)
    monkeypatch.setattr(delegate_tool, "_STALE_RESULT_GRACE_SECONDS", grace, raising=False)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: None)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)


def test_frozen_activity_stale_heartbeat_returns_instead_of_hanging(monkeypatch):
    child = FrozenChild()
    _fast_watchdog(monkeypatch)
    started = time.monotonic()
    result = delegate_tool._run_single_child(0, "hang", child=child, parent_agent=_parent())
    assert time.monotonic() - started < 5
    assert result["status"] == "timeout"
    assert "no progress" in (result.get("error") or "")


def test_stale_then_real_result_is_collected(monkeypatch):
    child = FrozenChild()
    _fast_watchdog(monkeypatch, grace=0.5)
    timer = threading.Timer(0.08, child.release.set)
    timer.start()
    try:
        result = delegate_tool._run_single_child(0, "finish", child=child, parent_agent=_parent())
    finally:
        timer.cancel()
        child.release.set()
    assert result["status"] == "completed"
    assert result["summary"] == "DONE"


def test_advancing_activity_is_not_timed_out(monkeypatch):
    child = FrozenChild()
    _fast_watchdog(monkeypatch)

    def advancing():
        return {"api_call_count": 1, "current_tool": None, "last_activity_ts": time.time()}

    child.get_activity_summary = advancing
    timer = threading.Timer(0.25, child.release.set)
    timer.start()
    try:
        result = delegate_tool._run_single_child(0, "live", child=child, parent_agent=_parent())
    finally:
        timer.cancel()
        child.release.set()
    assert result["status"] == "completed"
    assert result["summary"] == "DONE"


def test_async_batch_collects_finished_sibling_when_peer_goes_stale(monkeypatch):
    """honor_parent_interrupt=False is the background/async runner; it must still unblock."""
    _fast_watchdog(monkeypatch)
    frozen = FrozenChild()
    done = CompletingChild()
    parent = _parent()
    batch = _Batch(
        task_list=[{"goal": "hang"}, {"goal": "done"}],
        children=[(0, {"goal": "hang"}, frozen), (1, {"goal": "done"}, done)],
        parent_agent=parent,
        creds={"model": "test"},
        context=None,
        top_role="leaf",
        max_children=2,
        live_deleg_id=None,
        live_writers=[None, None],
        live_paths=[],
        origin_wake_sid="",
        origin_ui_session_id="",
        origin_owner_transport=None,
        origin_owner_session_record=None,
        origin_session_history_delivery=False,
        overall_start=time.monotonic(),
    )
    started = time.monotonic()
    combined = _execute_and_aggregate(batch, honor_parent_interrupt=False)
    assert time.monotonic() - started < 5
    by_index = {entry["task_index"]: entry for entry in combined["results"]}
    assert by_index[1]["status"] == "completed"
    assert by_index[1]["summary"] == "SIBLING"
    assert by_index[0]["status"] == "timeout"

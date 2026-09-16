"""Regression tests for dashboard eager-reconcile thread ownership."""

from __future__ import annotations

import threading
import time

import hermes_cli.web_server as web_server


SLOW_WORKER_SECONDS = 1.5


def _patch_fast_lifespan(monkeypatch):
    monkeypatch.setattr(web_server, "_warm_gateway_module", lambda: None)
    monkeypatch.setattr(
        "tui_gateway.methods_groups.start_hosted_room_service", lambda: None
    )
    monkeypatch.setattr(
        "tui_gateway.methods_groups.stop_hosted_room_service",
        lambda **_kwargs: None,
    )


def _alive_reconcile_threads():
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "statedb-eager-reconcile" and thread.is_alive()
    ]


def test_lifespan_waits_for_eager_reconcile_before_shutdown(monkeypatch):
    from fastapi.testclient import TestClient

    _patch_fast_lifespan(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def worker():
        started.set()
        release.wait(timeout=5.0)

    monkeypatch.setattr(web_server, "_eager_reconcile_own_session_db", worker)

    with TestClient(web_server.app, raise_server_exceptions=False):
        assert started.wait(timeout=5.0)
        assert _alive_reconcile_threads(), (
            "lifespan must start statedb-eager-reconcile before serving"
        )
        release.set()

    assert not _alive_reconcile_threads(), (
        "lifespan shutdown must join statedb-eager-reconcile so it cannot "
        "outlive the app"
    )


def test_sequential_lifespans_do_not_accumulate_reconcile_threads(monkeypatch):
    from fastapi.testclient import TestClient

    _patch_fast_lifespan(monkeypatch)
    started = threading.Event()

    def worker():
        started.set()
        time.sleep(SLOW_WORKER_SECONDS)

    monkeypatch.setattr(web_server, "_eager_reconcile_own_session_db", worker)

    with TestClient(web_server.app, raise_server_exceptions=False):
        assert started.wait(timeout=5.0)
    started.clear()
    with TestClient(web_server.app, raise_server_exceptions=False):
        assert started.wait(timeout=5.0)

    assert not _alive_reconcile_threads()

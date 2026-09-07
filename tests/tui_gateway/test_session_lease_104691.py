from __future__ import annotations

import threading
from pathlib import Path

from hermes_cli.active_sessions import active_session_registry_snapshot
from tui_gateway import server


def test_detached_idle_desktop_lane_does_not_keep_lease_when_orphan_reap_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    """A parked, idle lane must not retain the writer lease forever (#104691)."""
    profile_home = tmp_path / "profile-home"
    transport = object()
    session_key = "torn-down-lane"
    lease, error = server._claim_active_session_slot(
        session_key,
        live_session_id="desktop-runtime",
        surface="desktop",
        profile_home=profile_home,
    )
    assert lease is not None and error is None

    session = {
        "active_session_lease": lease,
        "close_on_disconnect": False,
        "history": [],
        "history_lock": threading.Lock(),
        "profile_home": str(profile_home),
        "running": False,
        "session_key": session_key,
        "source": "desktop",
        "transport": transport,
        "viewers": {},
    }
    monkeypatch.setattr(server, "_sessions", {"runtime": session})
    monkeypatch.setattr(server, "_pending_ws_reaps", {})
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0.0)

    try:
        assert server._close_sessions_for_transport(transport) == (0, 1)
        assert session["transport"] is server._detached_ws_transport
        assert active_session_registry_snapshot(registry_home=profile_home) == []
        replacement, replacement_error = server._claim_active_session_slot(
            session_key,
            live_session_id="replacement-runtime",
            surface="desktop",
            profile_home=profile_home,
        )
        assert replacement is not None and replacement_error is None
        replacement.release()
    finally:
        lease.release()
        server._sessions.clear()


def test_detached_running_desktop_lane_keeps_lease_when_orphan_reap_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    """Disabling orphan reaping must not release a lease for an active turn."""
    profile_home = tmp_path / "profile-home"
    transport = object()
    lease, error = server._claim_active_session_slot(
        "running-lane",
        live_session_id="desktop-runtime",
        surface="desktop",
        profile_home=profile_home,
    )
    assert lease is not None and error is None
    session = {
        "active_session_lease": lease,
        "close_on_disconnect": False,
        "history": [],
        "history_lock": threading.Lock(),
        "profile_home": str(profile_home),
        "running": True,
        "session_key": "running-lane",
        "source": "desktop",
        "transport": transport,
        "viewers": {},
    }
    monkeypatch.setattr(server, "_sessions", {"runtime": session})
    monkeypatch.setattr(server, "_pending_ws_reaps", {})
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0.0)

    try:
        assert server._close_sessions_for_transport(transport) == (0, 1)
        entries = active_session_registry_snapshot(registry_home=profile_home)
        assert [entry["session_id"] for entry in entries] == ["running-lane"]
    finally:
        lease.release()
        server._sessions.clear()

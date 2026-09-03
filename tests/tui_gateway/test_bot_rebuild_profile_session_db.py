"""In-place agent rebuilds must keep a session on its OWN profile ``state.db``.

``_make_agent`` defaults ``session_db`` to ``_get_db()`` — a module-level
singleton memoized on the LAUNCH profile for the life of the process. The build
paths that carry a ``profile_home`` (the deferred builder, ``session.resume``,
branch, compute host) all open that profile's store and pass it, but the two
sites that rebuild an agent *for a session that already exists* did not:

* ``_sync_bot_capabilities`` — every turn start of a Bot Chat whose capability
  fingerprint moved (a skill install, an MCP toggle, a SOUL edit, or simply
  another profile being created, since the fingerprint spans ``profiles/``).
* ``_reset_session_agent`` — ``/new``, and the toolset/MCP change handler in
  ``methods_tools``.

Both run with the turn's ``HERMES_HOME`` override already bound, so
``run_agent._ensure_db_session`` stamped the row with the NAMED profile while
writing it through the launch handle: the profile's own store stayed empty and
the launch store grew a visible duplicate Desktop could resume under the wrong
persona (#101719).

Pinned here: the rebuilds reuse the live handle, sole ownership of a dedicated
handle moves with it (nothing else ever closes the replaced agent), the shared
launch handle is never claimed, and ``_make_agent`` fails closed rather than
silently binding the launch store for a profile-scoped session.
"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server


class _RecordingDB:
    """Stand-in for ``hermes_state.SessionDB`` that counts ``close()`` calls."""

    def __init__(self, label="db"):
        self.label = label
        self.closed = 0

    def close(self):
        self.closed += 1

    def end_session(self, *_a, **_k):
        pass


@pytest.fixture
def launch_db(monkeypatch):
    """The process-wide launch handle ``_make_agent`` would fall back to."""
    db = _RecordingDB("launch")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    return db


@pytest.fixture
def registered(monkeypatch):
    """Register a session in ``server._sessions`` for the duration of a test."""
    monkeypatch.setattr(server, "_sessions", {})

    def _register(sid, session):
        server._sessions[sid] = session
        return session

    return _register


def _session(profile_home, agent):
    return {
        "session_key": "bot-key",
        "profile_home": profile_home,
        "agent": agent,
        "history": [],
        "history_lock": threading.Lock(),
    }


def _live_agent(db, *, owns):
    return types.SimpleNamespace(
        _session_db=db,
        _owns_session_db=owns,
        _session_title_hint="Bot Chat",
    )


@pytest.fixture
def rebuild_capture(monkeypatch):
    """Capture what ``_make_agent`` is handed, returning a fresh stand-in."""
    captured: dict = {}

    def _fake_make_agent(sid, key, session_db=None, **kwargs):
        captured["sid"] = sid
        captured["session_db"] = session_db
        captured.update(kwargs)
        captured["new_agent"] = types.SimpleNamespace(
            _session_db=session_db, _owns_session_db=False
        )
        return captured["new_agent"]

    monkeypatch.setattr(server, "_make_agent", _fake_make_agent)
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_source", lambda *a, **k: "desktop")
    monkeypatch.setattr(server, "_config_model_target", lambda: ("m", "p"))
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    return captured


@pytest.fixture
def capability_change(monkeypatch):
    """Make ``capability_fingerprint`` report a surface that just moved."""
    import tools.bot_mode_probe as probe

    monkeypatch.setattr(probe, "capability_fingerprint", lambda _home: "after")


# ---------------------------------------------------------------------------
# 1. The capability rebuild (_sync_bot_capabilities)
# ---------------------------------------------------------------------------


def test_capability_rebuild_reuses_the_live_profile_handle(
    launch_db, registered, rebuild_capture, capability_change, tmp_path
):
    """The bug: the rebuild reached ``_get_db()`` — the launch store."""
    profile_db = _RecordingDB("profile")
    agent = _live_agent(profile_db, owns=True)
    session = _session(str(tmp_path / "profiles" / "named"), agent)
    session["bot_caps_seen"] = "before"
    registered("sid-bot", session)

    server._sync_bot_capabilities("sid-bot", session)

    assert rebuild_capture["session_db"] is profile_db
    assert rebuild_capture["session_db"] is not launch_db
    assert session["agent"] is rebuild_capture["new_agent"]


def test_capability_rebuild_moves_dedicated_handle_ownership(
    launch_db, registered, rebuild_capture, capability_change, tmp_path
):
    """The replaced agent is dropped without close(); teardown only reaches the
    session's CURRENT agent, so the single owner has to move with the handle."""
    profile_db = _RecordingDB("profile")
    agent = _live_agent(profile_db, owns=True)
    session = _session(str(tmp_path / "profiles" / "named"), agent)
    session["bot_caps_seen"] = "before"
    registered("sid-bot", session)

    server._sync_bot_capabilities("sid-bot", session)

    assert agent._owns_session_db is False
    assert session["agent"]._owns_session_db is True
    assert profile_db.closed == 0


def test_capability_rebuild_never_claims_the_shared_launch_handle(
    launch_db, registered, rebuild_capture, capability_change
):
    """A launch-profile Bot Chat holds the shared handle, which outlives every
    agent — claiming it would tear down every other chat on session.close."""
    agent = _live_agent(launch_db, owns=False)
    session = _session(None, agent)
    session["bot_caps_seen"] = "before"
    registered("sid-launch", session)

    server._sync_bot_capabilities("sid-launch", session)

    assert rebuild_capture["session_db"] is launch_db
    assert session["agent"]._owns_session_db is False
    assert launch_db.closed == 0


def test_capability_rebuild_keeps_the_live_agent_when_the_build_fails(
    launch_db, registered, capability_change, monkeypatch, tmp_path
):
    """A refused/failed rebuild must leave the session on its correct store."""
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_source", lambda *a, **k: "desktop")
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no store")),
    )
    profile_db = _RecordingDB("profile")
    agent = _live_agent(profile_db, owns=True)
    session = _session(str(tmp_path / "profiles" / "named"), agent)
    session["bot_caps_seen"] = "before"
    registered("sid-bot", session)

    server._sync_bot_capabilities("sid-bot", session)

    assert session["agent"] is agent
    assert agent._owns_session_db is True


# ---------------------------------------------------------------------------
# 2. The /new rebuild (_reset_session_agent)
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_env(monkeypatch):
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_source", lambda *a, **k: "desktop")
    monkeypatch.setattr(server, "_config_model_target", lambda: ("m", "p"))
    monkeypatch.setattr(server, "_context_cwd_is_launch_artifact", lambda *a: False)
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "compact")
    monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *a, **k: None)


def test_reset_session_agent_reuses_the_live_profile_handle(
    launch_db, registered, rebuild_capture, reset_env, tmp_path
):
    """``/new`` keeps the session — and therefore its store."""
    profile_db = _RecordingDB("profile")
    agent = _live_agent(profile_db, owns=True)
    session = _session(str(tmp_path / "profiles" / "named"), agent)
    registered("sid-new", session)

    server._reset_session_agent("sid-new", session)

    assert rebuild_capture["session_db"] is profile_db
    assert rebuild_capture["session_db"] is not launch_db
    assert agent._owns_session_db is False
    assert session["agent"]._owns_session_db is True


def test_reset_session_agent_leaves_a_launch_session_on_the_shared_handle(
    launch_db, registered, rebuild_capture, reset_env
):
    agent = _live_agent(launch_db, owns=False)
    session = _session(None, agent)
    registered("sid-new-launch", session)

    server._reset_session_agent("sid-new-launch", session)

    assert rebuild_capture["session_db"] is launch_db
    assert session["agent"]._owns_session_db is False


def test_reset_opens_the_profile_store_when_no_agent_was_ever_built(
    launch_db, registered, rebuild_capture, reset_env, monkeypatch, tmp_path
):
    """``tools.configure`` can beat a deferred build to the punch.

    It calls ``_reset_session_agent`` on any live session record, so a profile
    Bot Chat opened but not yet spoken to has ``agent=None`` and no handle to
    reuse. The rebuild must open that profile's store rather than fall through
    to the launch handle — and must not raise, which is what a bare
    fail-closed guard would have done to a working RPC.
    """
    opened: list[_RecordingDB] = []

    def _open(profile_home):
        db = _RecordingDB(f"opened:{profile_home}")
        opened.append(db)
        return db

    monkeypatch.setattr(server, "_open_profile_session_db", _open)
    session = _session(str(tmp_path / "profiles" / "named"), None)
    registered("sid-cold", session)

    server._reset_session_agent("sid-cold", session)

    assert len(opened) == 1
    assert rebuild_capture["session_db"] is opened[0]
    assert rebuild_capture["session_db"] is not launch_db
    # The freshly opened handle has no other owner — the new agent takes it.
    assert session["agent"]._owns_session_db is True
    assert opened[0].closed == 0


def test_reset_closes_a_self_opened_handle_when_the_build_fails(
    launch_db, registered, reset_env, monkeypatch, tmp_path
):
    """A handle nothing took must not outlive the failed rebuild."""
    opened: list[_RecordingDB] = []

    def _open(profile_home):
        db = _RecordingDB("opened")
        opened.append(db)
        return db

    monkeypatch.setattr(server, "_open_profile_session_db", _open)
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    session = _session(str(tmp_path / "profiles" / "named"), None)
    registered("sid-cold-fail", session)

    with pytest.raises(RuntimeError, match="build failed"):
        server._reset_session_agent("sid-cold-fail", session)

    assert opened[0].closed == 1


def test_capability_rebuild_closes_a_self_opened_handle_when_the_build_fails(
    launch_db, registered, capability_change, monkeypatch, tmp_path
):
    opened: list[_RecordingDB] = []

    def _open(profile_home):
        db = _RecordingDB("opened")
        opened.append(db)
        return db

    monkeypatch.setattr(server, "_open_profile_session_db", _open)
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_source", lambda *a, **k: "desktop")
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("build failed")),
    )
    # A live agent with no handle at all (a degraded db) still names a profile.
    agent = _live_agent(None, owns=False)
    session = _session(str(tmp_path / "profiles" / "named"), agent)
    session["bot_caps_seen"] = "before"
    registered("sid-degraded", session)

    server._sync_bot_capabilities("sid-degraded", session)

    assert opened[0].closed == 1
    assert session["agent"] is agent


# ---------------------------------------------------------------------------
# 3. The root-cause guard (_make_agent's silent launch-store default)
# ---------------------------------------------------------------------------


def test_make_agent_refuses_the_launch_store_for_a_profile_session(
    registered, tmp_path
):
    """Fail closed: a forgotten handle must not become a silent mis-persist."""
    home = str(tmp_path / "profiles" / "named")
    registered("sid-scoped", _session(home, None))

    with pytest.raises(RuntimeError, match="profile-scoped agent"):
        server._require_profile_session_db("sid-scoped", None)


def test_make_agent_allows_the_launch_store_for_a_launch_session(registered):
    registered("sid-plain", _session(None, None))

    server._require_profile_session_db("sid-plain", None)  # must not raise


def test_make_agent_allows_an_explicit_handle_for_a_profile_session(
    registered, tmp_path
):
    registered("sid-ok", _session(str(tmp_path / "profiles" / "named"), None))

    server._require_profile_session_db("sid-ok", _RecordingDB("profile"))


def test_make_agent_allows_an_unregistered_session(registered):
    """The compute host builds before the session is registered; it passes its
    own handle explicitly, and an unknown sid must not become a hard failure."""
    server._require_profile_session_db("sid-unknown", None)  # must not raise


# ---------------------------------------------------------------------------
# 4. Ownership transfer invariants
# ---------------------------------------------------------------------------


def test_ownership_move_is_a_noop_when_the_old_agent_owned_nothing():
    db = _RecordingDB("borrowed")
    old = types.SimpleNamespace(_session_db=db, _owns_session_db=False)
    new = types.SimpleNamespace(_session_db=db, _owns_session_db=False)

    server._move_session_db_ownership(old, new, db)

    assert new._owns_session_db is False


def test_ownership_move_keeps_the_old_owner_when_the_transfer_is_refused():
    """A refused transfer (the new agent is not holding this handle) must not
    leave the handle ownerless."""
    db = _RecordingDB("profile")
    old = types.SimpleNamespace(_session_db=db, _owns_session_db=True)
    new = types.SimpleNamespace(
        _session_db=_RecordingDB("other"), _owns_session_db=False
    )

    server._move_session_db_ownership(old, new, db)

    assert old._owns_session_db is True
    assert new._owns_session_db is False

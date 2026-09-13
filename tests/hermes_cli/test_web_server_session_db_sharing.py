"""Regression tests for #110276: _open_session_db_at_path reuses shared registry
instance when active, preventing multi-handle split and POSIX advisory lock drops."""

from pathlib import Path
import pytest
import hermes_state_registry as registry
from hermes_cli.web_server_sessions import _open_session_db_at_path


def test_open_session_db_at_path_reuses_shared_generation(tmp_path):
    db_path = tmp_path / "state.db"

    # With shared handle active in registry
    primary_db = registry.acquire(db_path)
    try:
        stats = registry.stats()
        assert stats["total_refcounts"] == 1

        # Read-only open should reuse the active shared generation
        ro_db = _open_session_db_at_path(db_path, read_only=True)
        assert ro_db is primary_db
        stats = registry.stats()
        assert stats["total_refcounts"] == 2

        # Closing ro_db decrements refcount, leaving primary_db intact
        ro_db.close()
        assert primary_db._conn is not None
        stats = registry.stats()
        assert stats["total_refcounts"] == 1
    finally:
        registry.release_or_close(primary_db)


def test_open_session_db_at_path_fallback_when_inactive(tmp_path):
    db_path = tmp_path / "inactive_state.db"

    # When no shared handle is active, opens a dedicated read-only SessionDB
    ro_db = _open_session_db_at_path(db_path, read_only=True)
    try:
        assert ro_db.read_only is True
        assert not registry.has_live_generation(db_path)
    finally:
        ro_db.close()


@pytest.mark.asyncio
async def test_append_system_metrics_reuses_shared_generation(tmp_path, monkeypatch):
    from hermes_cli.web_routers.status import _advisory_pressure

    db_path = tmp_path / "state.db"
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    primary_db = registry.acquire(db_path)
    try:
        status = {}
        await _advisory_pressure(status, tmp_path)
        # Primary DB remains open and active
        assert primary_db._conn is not None
        stats = registry.stats()
        assert stats["total_refcounts"] == 1
    finally:
        registry.release_or_close(primary_db)



"""Regression test for #110276: _profile_session_fields borrows active shared SessionDB."""

from pathlib import Path
import hermes_state_registry as registry
from tui_gateway.methods_profiles import _profile_session_fields


def test_profile_session_fields_borrows_active_shared_db(tmp_path):
    profile_dir = tmp_path / "test_profile"
    profile_dir.mkdir(parents=True)
    db_path = profile_dir / "state.db"

    # Simulate active profile session DB in registry
    active_db = registry.acquire(db_path)
    try:
        stats = registry.stats()
        assert stats["total_refcounts"] == 1

        row = {}
        _profile_session_fields(row, profile_dir)

        # Active db must remain open with its connection intact and refcount restored
        assert active_db._conn is not None
        stats = registry.stats()
        assert stats["total_refcounts"] == 1
    finally:
        registry.release_or_close(active_db)

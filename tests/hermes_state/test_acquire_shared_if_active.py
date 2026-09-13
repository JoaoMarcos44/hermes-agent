"""Regression tests for #110276: acquire_shared_if_active, has_live_generation,
and proxy unwrapping in hermes_state_registry."""

from pathlib import Path
import hermes_state_registry as registry
from cron.scheduler import _BoundedCronSessionDB


def test_acquire_shared_if_active_lifecycle(tmp_path):
    db_path = tmp_path / "state.db"

    # Initially inactive
    assert not registry.has_live_generation(db_path)
    assert registry.acquire_shared_if_active(db_path) is None

    # Acquired primary handle
    db1 = registry.acquire(db_path)
    assert registry.has_live_generation(db_path)

    stats = registry.stats()
    assert stats["live_generations"] == 1
    assert stats["total_refcounts"] == 1

    # acquire_shared_if_active borrows the live handle and increments refcount
    db2 = registry.acquire_shared_if_active(db_path)
    assert db2 is db1
    stats = registry.stats()
    assert stats["total_refcounts"] == 2

    # Closing db2 decrements refcount without closing db1
    db2.close()
    assert db1._conn is not None
    stats = registry.stats()
    assert stats["total_refcounts"] == 1

    # Closing db1 releases final refcount and closes
    db1.close()
    assert db1._conn is None
    assert not registry.has_live_generation(db_path)


def test_release_unwraps_proxy(tmp_path):
    db_path = tmp_path / "proxy_test.db"

    db = registry.acquire(db_path)
    proxy = _BoundedCronSessionDB(db, "job-123")

    # release and release_or_close should succeed on proxy
    assert registry.release(proxy)
    stats = registry.stats()
    assert stats["total_refcounts"] == 0
    assert not registry.has_live_generation(db_path)

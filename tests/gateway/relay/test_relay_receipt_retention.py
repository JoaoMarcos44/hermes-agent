"""Relay settlement receipts retain replay protection without immortal RAM/disk entries."""

import sqlite3

from gateway.config import GatewayConfig
from gateway.session import SessionStore


def test_receipt_capacity_does_not_shorten_durable_retention(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    cap, retention = 4, 60.0
    monkeypatch.setattr(SessionStore, "_RELAY_RECEIPT_CACHE_MAX", cap, raising=False)
    monkeypatch.setattr(SessionStore, "_RELAY_RECEIPT_RETENTION_SECONDS", retention, raising=False)
    monkeypatch.setattr(SessionStore, "_relay_receipt_now", staticmethod(lambda: clock[0]), raising=False)

    store = SessionStore(tmp_path, GatewayConfig())
    db = store._routing_db
    db.set_meta("relay_delivery_settled:v1:legacy", "1")
    db.set_meta("unrelated:receipt-test", "keep")
    keys = [f"old:{n}" for n in range(cap * 2 + 1)]
    for key in keys:
        store.mark_relay_delivery_settled(key)
    assert len(store._settled_relay_deliveries) <= cap
    assert store.is_relay_delivery_settled(keys[0]), "RAM eviction must preserve recent disk receipts"
    assert store.is_relay_delivery_settled("legacy"), "legacy receipts receive a migration grace period"
    store.close_all_db_handles()
    assert db._conn is None, "reopen must use a physically closed database"

    clock[0] += retention / 2
    store = SessionStore(tmp_path, GatewayConfig())
    recent_keys = [f"recent:{n}" for n in range(cap * 2 + 1)]
    for key in recent_keys:
        assert store.mark_relay_delivery_settled(key)
    assert store.is_relay_delivery_settled(keys[0])
    assert store.is_relay_delivery_settled(recent_keys[0])
    assert len(store._settled_relay_deliveries) <= cap
    store.close_all_db_handles()

    clock[0] += retention / 2 + 1
    store = SessionStore(tmp_path, GatewayConfig())
    assert not store.is_relay_delivery_settled(keys[0])
    assert not store.is_relay_delivery_settled("legacy")
    assert all(store.is_relay_delivery_settled(key) for key in recent_keys)
    assert len(store._settled_relay_deliveries) <= cap
    db = store._routing_db
    assert not db.list_meta_prefix("relay_delivery_settled:v1:")
    assert len(db.list_meta_prefix("relay_delivery_settled:v2:")) == len(recent_keys)
    assert db.get_meta("unrelated:receipt-test") == "keep"
    clock[0] += retention
    assert not store.is_relay_delivery_settled(recent_keys[-1]), "RAM hits must expire too"
    assert not db.list_meta_prefix("relay_delivery_settled:v2:")
    store.close_all_db_handles()


def test_failed_receipt_write_never_reports_durable_settlement(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, GatewayConfig())
    store.close_all_db_handles()
    store._db = None
    assert store.mark_relay_delivery_settled("no-db") is False
    assert not store.is_relay_delivery_settled("no-db")

    store = SessionStore(tmp_path, GatewayConfig())
    db = store._routing_db
    assert not store.is_relay_delivery_settled("failed-write")

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("receipt write unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(db, "_execute_write", unavailable)
        assert store.mark_relay_delivery_settled("failed-write") is False
    assert not store.is_relay_delivery_settled("failed-write")
    assert store.mark_relay_delivery_settled("recovered") is True
    db.set_meta("relay_delivery_settled:v1:blocked-migration", "1")
    store.close_all_db_handles()
    reopened = SessionStore(tmp_path, GatewayConfig())
    with monkeypatch.context() as failure:
        failure.setattr(reopened._routing_db, "_execute_write", unavailable)
        assert not reopened.is_relay_delivery_settled("failed-write")
        assert reopened.is_relay_delivery_settled("recovered"), "pruning failure must not hide committed receipts"
        assert reopened.is_relay_delivery_settled("blocked-migration")
        assert "blocked-migration" not in reopened._settled_relay_deliveries, "unknown legacy age must not be cached"
    reopened.close_all_db_handles()

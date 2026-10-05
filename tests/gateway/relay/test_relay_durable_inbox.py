"""Durable relay custody preserves every pending payload until committed settlement."""

import json
import sqlite3

from gateway.config import GatewayConfig
from gateway.session import SessionStore


def test_pending_payload_survives_reopen_age_duplicate_and_page_cleanup(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(SessionStore, "_relay_receipt_now", staticmethod(lambda: clock[0]))
    store = SessionStore(tmp_path, GatewayConfig())
    keys = [f"delivery:{n}" for n in range(5)]
    payload = {"version": 1, "event": {"text": "original", "media_urls": ["cache/image.png"]}}
    for key in keys:
        assert store.stage_relay_delivery(key, payload)
    assert "admitted_at" not in payload, "storage must not mutate the caller's event snapshot"
    assert store.stage_relay_delivery(keys[0], {"version": 1, "event": {"text": "replacement"}})
    original = store.get_relay_delivery_pending(keys[0])
    assert original["event"] == payload["event"]
    db = store._routing_db
    store.close_all_db_handles()
    assert db._conn is None

    clock[0] += store._RELAY_RECEIPT_RETENTION_SECONDS * 2
    store = SessionStore(tmp_path, GatewayConfig())
    assert store.get_relay_delivery_pending(keys[0]) == original
    assert store.mark_relay_delivery_settled("unrelated-completion")
    first = store.pending_relay_deliveries(limit=2)
    assert [key for key, _ in first] == keys[:2]
    cursor = (first[-1][1]["admitted_at"], first[-1][0])
    for key, _ in first:
        assert store.mark_relay_delivery_settled(key)
    second = store.pending_relay_deliveries(limit=2, after=cursor)
    assert [key for key, _ in second] == keys[2:4], "deleted cursor rows must not break paging"
    assert store.pending_relay_deliveries(limit=2, after=(second[-1][1]["admitted_at"], second[-1][0]))[0][0] == keys[-1]
    assert store.get_relay_delivery_pending(keys[0]) is None
    assert store.get_relay_delivery_pending(keys[-1])["event"] == payload["event"]
    assert not store.clear_relay_delivery_pending(keys[-1]), "unsettled input must not be deleted"
    clock[0] += store._RELAY_RECEIPT_RETENTION_SECONDS + 1
    assert store.stage_relay_delivery(keys[0], {"event": {"text": "renewed after retention"}})
    assert store.get_relay_delivery_pending(keys[0])["event"]["text"] == "renewed after retention"
    assert keys[0] in dict(store.pending_relay_deliveries()), "expired completion must not hide new custody"
    store.close_all_db_handles()


def test_settlement_and_pending_removal_are_failure_atomic(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, GatewayConfig())
    store.close_all_db_handles()
    store._db = None
    assert store.stage_relay_delivery("no-db", {"event": {"text": "keep"}}) is False
    assert store.get_relay_delivery_pending("no-db") is None
    assert store.pending_relay_deliveries() == []
    assert store.clear_relay_delivery_pending("no-db") is False

    store = SessionStore(tmp_path, GatewayConfig())
    assert store.stage_relay_delivery("atomic", {"event": {"text": "keep"}})
    original = store.get_relay_delivery_pending("atomic")
    assert not store.is_relay_delivery_settled("atomic")
    db = store._routing_db
    execute_write = db._execute_write

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("injected unavailable writer")

    with monkeypatch.context() as failure:
        failure.setattr(db, "_execute_write", unavailable)
        assert store.stage_relay_delivery("failed-stage", {"event": {"text": "keep"}}) is False
    assert store.get_relay_delivery_pending("failed-stage") is None

    class CleanupFailure:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            if sql.startswith("DELETE"):
                raise sqlite3.OperationalError("injected pending deletion failure")
            return self.conn.execute(sql, params)

    def fail_cleanup(fn, *args, **kwargs):
        return execute_write(lambda conn: fn(CleanupFailure(conn)), *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(db, "_execute_write", fail_cleanup)
        assert store.mark_relay_delivery_settled("atomic") is False
    assert store.get_relay_delivery_pending("atomic") == original
    assert not store.is_relay_delivery_settled("atomic"), "receipt insertion must roll back with failed deletion"
    assert store.mark_relay_delivery_settled("atomic") is True
    assert store.get_relay_delivery_pending("atomic") is None
    store.close_all_db_handles()
    reopened = SessionStore(tmp_path, GatewayConfig())
    assert reopened.is_relay_delivery_settled("atomic")
    assert reopened.get_relay_delivery_pending("atomic") is None
    reopened._routing_db.set_meta("relay_delivery_pending:v1:atomic", json.dumps(original))
    assert reopened.pending_relay_deliveries() == [], "recovery must skip a committed completed delivery"
    assert reopened.clear_relay_delivery_pending("atomic") is True
    assert reopened.get_relay_delivery_pending("atomic") is None
    reopened.close_all_db_handles()

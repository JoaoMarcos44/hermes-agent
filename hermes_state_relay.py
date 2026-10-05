"""Timestamped relay settlement receipts in the gateway's owning state database."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


_RELAY_RECEIPT_PREFIX = "relay_delivery_settled:v2:"
_LEGACY_RELAY_RECEIPT_PREFIX = "relay_delivery_settled:v1:"
_RELAY_PENDING_PREFIX = "relay_delivery_pending:v1:"
# The connector publishes no maximum replay age. This is the gateway's local
# completed-delivery dedupe guarantee, not an expiry for unacknowledged inputs.
RELAY_RECEIPT_RETENTION_SECONDS = 30 * 86400.0


class SessionRelayReceiptsMixin:
    """Receipt operations use SessionDB's normal commit and generation guards."""

    def get_relay_delivery_receipt(self, dedupe_key: str, *, cutoff: float) -> Optional[float]:
        rows = self._read_all(
            "SELECT CAST(value AS REAL) FROM state_meta "
            "WHERE key = ? AND CAST(value AS REAL) > ?",
            (_RELAY_RECEIPT_PREFIX + dedupe_key, cutoff),
        )
        return float(rows[0][0]) if rows else None

    def set_relay_delivery_receipt(self, dedupe_key: str, *, settled_at: float) -> None:
        def settle(conn):
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_RELAY_RECEIPT_PREFIX + dedupe_key, str(settled_at)),
            )
            conn.execute("DELETE FROM state_meta WHERE key = ?", (_RELAY_PENDING_PREFIX + dedupe_key,))

        self._execute_write(settle)

    def stage_relay_delivery_pending(
        self, dedupe_key: str, payload: Dict[str, Any], *, admitted_at: int, cutoff: float,
    ) -> None:
        """Preserve the first committed snapshot; completed deliveries already have custody."""
        snapshot = dict(payload)
        snapshot["admitted_at"] = admitted_at
        serialized = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)

        def stage(conn):
            # The local completed-delivery retention can expire before a later
            # retransmission is admitted. Its old receipt must not hide new custody.
            conn.execute(
                "DELETE FROM state_meta WHERE key = ? AND CAST(value AS REAL) <= ?",
                (_RELAY_RECEIPT_PREFIX + dedupe_key, cutoff),
            )
            conn.execute(
                "INSERT OR IGNORE INTO state_meta (key, value) "
                "SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM state_meta WHERE key IN (?, ?))",
                (
                    _RELAY_PENDING_PREFIX + dedupe_key, serialized,
                    _RELAY_RECEIPT_PREFIX + dedupe_key, _LEGACY_RELAY_RECEIPT_PREFIX + dedupe_key,
                ),
            )
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (_RELAY_PENDING_PREFIX + dedupe_key,),
            ).fetchone()
            if row is not None:
                self._decode_relay_pending(row[0])  # corrupt prior custody must not earn an ACK

        self._execute_write(stage)

    @staticmethod
    def _decode_relay_pending(value: str) -> Dict[str, Any]:
        payload = json.loads(value)
        if not isinstance(payload, dict) or not isinstance(payload.get("admitted_at"), int):
            raise ValueError("invalid durable relay input snapshot")
        return payload

    def get_relay_delivery_pending(self, dedupe_key: str) -> Optional[Dict[str, Any]]:
        value = self.get_meta(_RELAY_PENDING_PREFIX + dedupe_key)
        return self._decode_relay_pending(value) if value is not None else None

    def pending_relay_deliveries(
        self, *, limit: int = 100, after: Optional[Tuple[int, str]] = None,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Bounded FIFO pages; the value cursor remains valid after rows are removed."""
        limit = max(1, min(int(limit), 1000))
        stamp, key = after if after is not None else (-1, "")
        rows = self._read_all(
            "SELECT p.key, p.value FROM state_meta p WHERE p.key >= ? AND p.key < ? "
            "AND (json_extract(p.value, '$.admitted_at'), p.key) > (?, ?) "
            "AND NOT EXISTS (SELECT 1 FROM state_meta r WHERE r.key IN "
            "(? || substr(p.key, ?), ? || substr(p.key, ?))) "
            "ORDER BY json_extract(p.value, '$.admitted_at'), p.key LIMIT ?",
            (
                _RELAY_PENDING_PREFIX, _RELAY_PENDING_PREFIX[:-1] + ";",
                stamp, _RELAY_PENDING_PREFIX + key,
                _RELAY_RECEIPT_PREFIX, len(_RELAY_PENDING_PREFIX) + 1,
                _LEGACY_RELAY_RECEIPT_PREFIX, len(_RELAY_PENDING_PREFIX) + 1, limit,
            ),
        )
        return [
            (row[0][len(_RELAY_PENDING_PREFIX):], self._decode_relay_pending(row[1]))
            for row in rows
        ]

    def clear_relay_delivery_pending(self, dedupe_key: str) -> bool:
        """Remove stale pending custody only when a committed settlement proves completion."""
        def clear(conn):
            settled = conn.execute(
                "SELECT 1 FROM state_meta WHERE key IN (?, ?) LIMIT 1",
                (_RELAY_RECEIPT_PREFIX + dedupe_key, _LEGACY_RELAY_RECEIPT_PREFIX + dedupe_key),
            ).fetchone()
            if settled is None:
                return False
            conn.execute("DELETE FROM state_meta WHERE key = ?", (_RELAY_PENDING_PREFIX + dedupe_key,))
            return True

        return self._execute_write(clear)

    def has_legacy_relay_delivery_receipt(self, dedupe_key: str) -> bool:
        """A committed legacy receipt remains evidence while migration writes are blocked."""
        return bool(self.get_meta(_LEGACY_RELAY_RECEIPT_PREFIX + dedupe_key))

    def prune_relay_delivery_receipts(self, *, now: float, retention_seconds: float) -> None:
        """Adopt legacy receipts once, then remove only expired receipts in one transaction.

        Legacy '1' values contain no settlement date. Starting their retention at
        adoption conservatively keeps replay protection through the upgrade grace
        period without leaving immortal records. The range scans use state_meta's
        primary-key index and never materialize the receipt collection in Python.
        """
        def maintain(conn):
            conn.execute(
                "INSERT OR IGNORE INTO state_meta (key, value) "
                "SELECT ? || substr(key, ?), ? FROM state_meta "
                "WHERE key >= ? AND key < ?",
                (
                    _RELAY_RECEIPT_PREFIX, len(_LEGACY_RELAY_RECEIPT_PREFIX) + 1,
                    str(now), _LEGACY_RELAY_RECEIPT_PREFIX,
                    _LEGACY_RELAY_RECEIPT_PREFIX[:-1] + ";",
                ),
            )
            conn.execute(
                "DELETE FROM state_meta WHERE key >= ? AND key < ?",
                (_LEGACY_RELAY_RECEIPT_PREFIX, _LEGACY_RELAY_RECEIPT_PREFIX[:-1] + ";"),
            )
            conn.execute(
                "DELETE FROM state_meta AS p WHERE p.key >= ? AND p.key < ? "
                "AND EXISTS (SELECT 1 FROM state_meta r WHERE r.key = ? || substr(p.key, ?))",
                (
                    _RELAY_PENDING_PREFIX, _RELAY_PENDING_PREFIX[:-1] + ";",
                    _RELAY_RECEIPT_PREFIX, len(_RELAY_PENDING_PREFIX) + 1,
                ),
            )
            conn.execute(
                "DELETE FROM state_meta WHERE key >= ? AND key < ? "
                "AND CAST(value AS REAL) <= ?",
                (
                    _RELAY_RECEIPT_PREFIX, _RELAY_RECEIPT_PREFIX[:-1] + ";",
                    now - retention_seconds,
                ),
            )

        self._execute_write(maintain)

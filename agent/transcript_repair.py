"""Transcript repair for SessionDB batch appends: reconcile in-memory assistant rows with committed SQLite
rows (blank-row in-place update, concurrent-winner adoption, watermark-compaction clone lookup) and sync
markers after commit."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List

from agent.context_compressor import _DB_PERSISTED_MARKER


def is_content_blank(content: Any) -> bool:
    """True when decoded message content is None, whitespace-only, or has no visible text parts."""
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return not "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text").strip()
    return False


def resolve_and_repair_transcript_batch(
    conn: sqlite3.Connection,
    session_id: str,
    messages: List[Dict[str, Any]],
    encode_content_fn: Callable[[Any], Any],
    decode_content_fn: Callable[[Any], Any],
) -> List[Dict[str, Any]]:
    """Partition a message batch within an active write transaction. An assistant message carrying an
    existing integer ``_row_id`` targets its active SQLite row (or the active clone a watermark compaction
    made of it): a blank row is updated in place; a non-blank one (concurrent winner) has its canonical
    content adopted without overwrite. Returns the messages that must be inserted as fresh rows."""
    inserted_rows: List[Dict[str, Any]] = []
    seen_ids = set()
    for msg in messages:
        if not isinstance(msg, dict):
            inserted_rows.append(msg)
            continue
        row = None
        existing_id = msg.get("_row_id")
        if isinstance(existing_id, int):
            row = conn.execute("SELECT * FROM messages WHERE session_id = ? AND active = 1 AND id = ?", (session_id, existing_id)).fetchone()
        tool_id = msg.get("tool_call_id")
        if row is None and isinstance(tool_id, str) and tool_id:
            row = conn.execute("SELECT * FROM messages WHERE session_id = ? AND active = 1 AND role = ? AND tool_call_id = ? ORDER BY id LIMIT 1", (session_id, msg.get("role", "unknown"), tool_id)).fetchone()
        if row is None and not (isinstance(tool_id, str) and tool_id):
            encoded = encode_content_fn(msg.get("content"))
            row = conn.execute("SELECT * FROM messages WHERE session_id = ? AND active = 1 AND role = ? AND timestamp IS ? AND content IS ? ORDER BY id LIMIT 1", (session_id, msg.get("role", "unknown"), msg.get("timestamp"), encoded)).fetchone()
        if row is None:
            inserted_rows.append(msg)
            continue
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        msg["_row_id"] = int(row["id"])
        decoded = decode_content_fn(row["content"])
        if msg.get("_repair_mutated"):
            conn.execute("UPDATE messages SET content = ?, tool_calls = ? WHERE id = ? AND session_id = ? AND active = 1", (encode_content_fn(msg.get("content")), __import__("json").dumps(msg.get("tool_calls")) if msg.get("tool_calls") is not None else None, row["id"], session_id))
        elif is_content_blank(decoded):
            conn.execute("UPDATE messages SET content = ? WHERE id = ? AND session_id = ? AND active = 1", (encode_content_fn(msg.get("content")), row["id"], session_id))
        else:
            msg["_canonical_content"] = decoded
    return inserted_rows


def _active_assistant_row(conn: sqlite3.Connection, session_id: str, row_id: int):
    """The active assistant row for ``row_id``, or the active clone a watermark compaction made of it."""
    row = conn.execute(
        "SELECT id, role, active, timestamp, content FROM messages "
        "WHERE id = ? AND session_id = ?",
        (row_id, session_id),
    ).fetchone()
    if row is None or row["role"] != "assistant":
        return None
    if int(row["active"] or 0) == 1:
        return row
    # Watermark compaction soft-archived the concurrent tail and cloned it.
    return conn.execute(
        "SELECT id, role, active, timestamp, content FROM messages "
        "WHERE session_id = ? AND active = 1 AND role = 'assistant' "
        "AND timestamp IS ? AND id != ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id, row["timestamp"], row["id"]),
    ).fetchone()


def sync_flushed_message_markers(batch_msgs: List[Dict[str, Any]], batch_rows: List[Dict[str, Any]]) -> None:
    """Stamp _DB_PERSISTED_MARKER and sync canonical row ID / content onto live dicts after commit."""
    for written, row in zip(batch_msgs, batch_rows):
        written[_DB_PERSISTED_MARKER] = True
        if isinstance(row.get("_row_id"), int):
            written["_row_id"] = row["_row_id"]
        if "_canonical_content" in row:
            written["content"] = row["_canonical_content"]


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Optional  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----

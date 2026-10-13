"""Shutdown must preserve admission order across slot, overflow and debounce owners."""

import pytest

from gateway.shutdown_flush import flush_overflow_to_file, recover_pending_spool
from hermes_state import SessionDB
from tests.gateway.test_drained_event_conservation import (
    KEY, _make_runner, _overflow, _text_event,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("transfer_buffer", [False, True])
async def test_shutdown_replays_admission_order_across_queue_owners(
    monkeypatch, tmp_path, transfer_buffer,
):
    runner, adapter = _make_runner(monkeypatch)
    a, b, c = (_text_event(text, text) for text in ("A pending", "B overflow", "C debounce"))
    c.preserve_prompt_pins = True
    assert runner._queue_or_replace_pending_event(KEY, a) is True
    assert runner._queue_or_replace_pending_event(KEY, b) is True
    await adapter._queue_text_debounce(KEY, c)
    if transfer_buffer:
        assert await adapter._flush_text_debounce_now(KEY) is True
    # Teardown visits adapter owners A/C before the runner-owned B tail.
    await adapter.cancel_background_tasks()
    assert flush_overflow_to_file({KEY: list(_overflow(runner))}) == (2 if transfer_buffer else 1)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.ensure_session("recovered", "gateway")
        recovered, held = recover_pending_spool(
            db, session_resolver=lambda key, not_after=None: ("recovered", db),
        )
        assert (recovered, held) == (3, set())
        assert [row["content"] for row in db.get_messages("recovered")] == [a.text, b.text, c.text]
    finally:
        db.close()

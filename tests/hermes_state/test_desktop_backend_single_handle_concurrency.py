"""Regression test for #110276:
Desktop backend (hermes serve) concurrency with cron ticker and polling API requests
must share the single SessionDB handle per path, never accumulating duplicate handles
or triggering DeletedWalGenerationError."""

import concurrent.futures
from pathlib import Path
import hermes_state_registry as registry
from hermes_state import SessionDB
from hermes_state_readpool import _read_budget_for, _HANDLES_PER_PATH_WARN
from hermes_cli.web_server_sessions import _open_session_db_at_path
from cron.scheduler import _BoundedCronSessionDB


def test_desktop_backend_concurrent_polls_and_cron_share_single_handle(tmp_path):
    db_path = tmp_path / "state.db"

    # 1. Simulate web_server._lifespan pinning launch database
    lifespan_db = registry.acquire(db_path)
    budget = _read_budget_for(db_path)

    # Initially exactly 1 live handle
    assert len(budget._members) == 1
    assert registry.has_live_generation(db_path)

    def simulate_api_poll(poll_id: int):
        # /api/status or /api/sessions poll: opens read-only and lists sessions
        db = _open_session_db_at_path(db_path, read_only=True)
        assert db is lifespan_db, f"Poll {poll_id} must reuse shared lifespan handle"
        _ = db.list_sessions_rich(limit=10, compact_rows=True)
        db.close()
        return True

    def simulate_cron_execution(job_id: str):
        # Cron job borrows registry handle, runs, wraps in _BoundedCronSessionDB and releases
        cron_db = registry.acquire(db_path)
        assert cron_db is lifespan_db
        session_id = f"cron-session-{job_id}"
        cron_db.create_session(session_id=session_id, source="cron")
        cron_db.set_session_title(session_id, f"Cron {job_id}")
        cron_db.append_message(
            session_id=session_id,
            role="assistant",
            content=f"Cron {job_id} executed",
        )
        proxy = _BoundedCronSessionDB(cron_db, job_id)
        registry.release_or_close(proxy)
        return True

    # 2. Run concurrent API polls and cron executions
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for i in range(20):
            futures.append(executor.submit(simulate_api_poll, i))
            if i % 4 == 0:
                futures.append(executor.submit(simulate_cron_execution, f"job-{i}"))

        results = [f.result() for f in concurrent.futures.as_completed(futures)]
        assert len(results) == 25
        assert all(results)

    # 3. Verify handle census after concurrent load
    # Must still have only 1 live handle and 0 warnings tripped
    assert len(budget._members) == 1
    assert not budget._duplicate_handles_warned

    # Verified that writes succeeded without WAL generation loss
    messages = lifespan_db.get_messages("cron-session-job-0")
    assert len(messages) == 1
    assert "Cron job-0 executed" in messages[0]["content"]

    # 4. Lifespan shutdown cleanly settles the handle
    registry.release_or_close(lifespan_db)
    assert not registry.has_live_generation(db_path)
    assert len(budget._members) == 0

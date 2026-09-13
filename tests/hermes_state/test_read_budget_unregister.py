"""Regression test for #110276: SessionDB.close() must immediately unregister
from _PathReadBudget so closed handles do not accumulate and trip multi-handle warnings."""

import logging
from hermes_state import SessionDB
from hermes_state_readpool import _read_budget_for, _HANDLES_PER_PATH_WARN


def test_session_db_close_unregisters_from_read_budget(tmp_path):
    db_path = tmp_path / "test.db"
    budget = _read_budget_for(db_path)

    db = SessionDB(db_path=db_path)
    assert db in budget._members

    db.close()
    assert db not in budget._members


def test_consecutive_session_db_opens_do_not_accumulate_or_warn(tmp_path, caplog):
    db_path = tmp_path / "test_consecutive.db"
    # Create the database file first so mode=ro opens succeed
    SessionDB(db_path=db_path).close()
    budget = _read_budget_for(db_path)

    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        # Open and close more handles than _HANDLES_PER_PATH_WARN sequentially
        for _ in range(_HANDLES_PER_PATH_WARN + 5):
            db = SessionDB(db_path=db_path, read_only=True)
            assert len(budget._members) == 1
            db.close()
            assert len(budget._members) == 0

    # Must NOT have logged the "live SessionDB handles" warning
    assert "live SessionDB handles" not in caplog.text
    assert budget._duplicate_handles_warned is False

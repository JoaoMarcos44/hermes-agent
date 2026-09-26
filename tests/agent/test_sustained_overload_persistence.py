"""#123167: sustained summary overloads are bounded across fresh-agent/session lifecycles."""

from __future__ import annotations

from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB


class StubProviderError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _err() -> StubProviderError:
    return StubProviderError(
        "Our servers are currently overloaded. Please try again later.",
        status_code=503,
    )


def _msgs(n: int = 12) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(n)
    ]


def _compressor(db: SessionDB, session_id: str, *, abort: bool = False) -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/model",
            quiet_mode=True,
            protect_first_n=2,
            protect_last_n=2,
            abort_on_summary_failure=abort,
        )
    compressor.summary_model = compressor.model
    compressor.bind_session_state(db, session_id)
    return compressor


def test_fresh_compressors_share_overload_budget_and_third_degrades(tmp_path):
    """Object-local counters restart at one on every bind; the durable counter must not."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="telegram")
    messages = _msgs()

    results = []
    for _ in range(3):
        compressor = _compressor(db, "s1")
        with patch("agent.context_compressor.call_llm", side_effect=_err()):
            results.append(compressor.compress(messages, current_tokens=999_999, force=True))

    assert results[0] == messages
    assert results[1] == messages
    assert results[2] != messages

    fresh = _compressor(db, "s1")
    assert fresh._consecutive_overload_aborts == 3
    assert fresh._last_summary_fallback_used is False

    with patch("agent.context_compressor.call_llm", side_effect=_err()):
        degraded = fresh.compress(messages, current_tokens=999_999, force=True)
    assert degraded != messages
    assert fresh._last_compress_aborted is False
    assert fresh._last_summary_fallback_used is True
    assert fresh._last_compression_telemetry["failure_class"] == "summary_overload_degraded"


def test_healthy_summary_resets_durable_budget(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="cli")
    db.set_compression_overload_abort_streak("s1", 2)

    compressor = _compressor(db, "s1")
    assert compressor._consecutive_overload_aborts == 2

    with patch("agent.context_compressor.call_llm", return_value="healthy summary"):
        result = compressor.compress(_msgs(), current_tokens=999_999, force=True)

    assert result != _msgs()
    assert db.get_compression_overload_abort_streak("s1") == 0
    assert _compressor(db, "s1")._consecutive_overload_aborts == 0


def test_abort_on_summary_failure_true_remains_hard_abort(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="cli")
    messages = _msgs()

    for _ in range(4):
        compressor = _compressor(db, "s1", abort=True)
        with patch("agent.context_compressor.call_llm", side_effect=_err()):
            result = compressor.compress(messages, current_tokens=999_999, force=True)
        assert result == messages
        assert compressor._last_compress_aborted is True
        assert compressor._last_summary_fallback_used is False


def test_compression_rotation_carries_overload_budget_to_child(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="telegram")
    db.set_compression_overload_abort_streak("parent", 2)

    compressor = _compressor(db, "parent")
    db.create_session("child", source="telegram", parent_session_id="parent")
    compressor.on_session_start(
        "child",
        boundary_reason="compression",
        old_session_id="parent",
        session_db=db,
    )

    assert compressor._consecutive_overload_aborts == 2
    assert db.get_compression_overload_abort_streak("child") == 2
    assert _compressor(db, "child")._consecutive_overload_aborts == 2


def test_atomic_increment_preserves_other_model_config(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s1", source="cli")
    db.patch_session_model_config("s1", {"keep": "value"})

    assert db.increment_compression_overload_abort_streak("s1") == 1
    assert db.increment_compression_overload_abort_streak("s1") == 2
    assert db.get_session_model_config_value("s1", "keep") == "value"

    db.set_compression_overload_abort_streak("s1", 0)
    assert db.get_compression_overload_abort_streak("s1") == 0
    assert db.get_session_model_config_value("s1", "keep") == "value"

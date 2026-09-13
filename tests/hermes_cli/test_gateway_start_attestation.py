"""Tests for the #91675 gateway-start honesty fixes.

Two holes closed by the fix:

1. ``_wait_for_gateway_ready`` returned on the FIRST process-table hit, so a
   gateway that spawned and then died moments later (parent Job Object
   teardown) still earned a ✓.  The poll now requires the gateway to stay
   visible for a confirmation window before it is reported ready.
2. A death AFTER the CLI process exits can never be seen by any poll.  Every
   ✓ now persists a start-attestation marker; the next CLI invocation checks
   it and reports the silent death (once) unless the lifecycle ledger shows
   a clean exit.

All timing knobs are shrunk so no test sleeps longer than ~1s.
"""

import json
import time

import pytest

import hermes_cli.gateway_windows as gateway_windows


# ---------------------------------------------------------------------------
# _wait_for_gateway_ready: confirmation window
# ---------------------------------------------------------------------------


def _install_pid_sequence(monkeypatch, snapshots):
    """find_gateway_pids returns successive snapshots (last one repeats)."""
    calls = {"n": 0}

    def _fake(*args, **kwargs):
        idx = min(calls["n"], len(snapshots) - 1)
        calls["n"] += 1
        return list(snapshots[idx])

    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", _fake)
    return calls


def test_ready_poll_rejects_gateway_that_dies_during_confirmation(monkeypatch):
    """First-hit-then-dead must NOT be reported ready (#91675 sabotage case).

    Pre-fix, the poll returned ``[4242]`` on the first snapshot and the CLI
    printed ✓ for a process that was already doomed.
    """
    _install_pid_sequence(monkeypatch, [[4242], [], [], []])
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda s: None)

    pids = gateway_windows._wait_for_gateway_ready(
        timeout_s=0.5, interval_s=0.01, confirm_s=0.2
    )
    assert pids == []


def test_ready_poll_confirms_stable_gateway(monkeypatch):
    """A gateway that stays visible through the confirmation window is ready."""
    _install_pid_sequence(monkeypatch, [[4242]])
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda s: None)

    pids = gateway_windows._wait_for_gateway_ready(
        timeout_s=0.5, interval_s=0.01, confirm_s=0.05
    )
    assert pids == [4242]


def test_ready_poll_recovers_when_gateway_respawns_within_deadline(monkeypatch):
    """Death during confirmation resumes polling; a later stable gateway wins."""
    # hit → dead (confirmation fails) → nothing → new stable pid
    _install_pid_sequence(monkeypatch, [[1], [], [], [2], [2], [2]])
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda s: None)

    pids = gateway_windows._wait_for_gateway_ready(
        timeout_s=1.0, interval_s=0.01, confirm_s=0.03
    )
    assert pids == [2]


def test_report_gateway_start_failure_is_loud_not_checkmark(monkeypatch, tmp_path, capsys):
    """No stable gateway ⇒ ✗ failure line, never ✓ (#91675)."""
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_hermes_home", lambda: str(tmp_path)
    )
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_x")

    gateway_windows._report_gateway_start("direct spawn (PID 7)")
    out = capsys.readouterr().out
    assert "✓" not in out
    assert "FAILED" in out
    assert "schtasks /Run /TN Hermes_Gateway_x" in out


# ---------------------------------------------------------------------------
# Start attestation: report-async-death on the next CLI invocation
# ---------------------------------------------------------------------------


@pytest.fixture
def attest_home(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: False)
    return tmp_path


def test_success_report_writes_attestation(monkeypatch, attest_home, capsys):
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda *a, **k: [321]
    )
    gateway_windows._LAST_SPAWN_BREAKAWAY_FALLBACK["fallback"] = False
    gateway_windows._report_gateway_start("direct spawn (PID 321)")
    assert "✓" in capsys.readouterr().out

    marker = attest_home / "state" / "gateway.start-attestation.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["pids"] == [321]
    assert data["via"] == "direct spawn (PID 321)"


def test_attestation_reports_silent_death(attest_home):
    """Attested PIDs gone + no clean-exit record ⇒ warning, marker consumed."""
    gateway_windows._write_start_attestation([555], "direct spawn (PID 555)")

    warning = gateway_windows.check_start_attestation(current_pids=[])
    assert warning is not None
    assert "died without a clean shutdown record" in warning
    assert "555" in warning
    # Consumed: second check is silent.
    assert gateway_windows.check_start_attestation(current_pids=[]) is None


def test_attestation_silent_when_gateway_running(attest_home):
    gateway_windows._write_start_attestation([555], "direct spawn (PID 555)")
    assert gateway_windows.check_start_attestation(current_pids=[555]) is None
    # Marker cleared — a later dead scan must not resurrect the warning.
    assert gateway_windows.check_start_attestation(current_pids=[]) is None


def test_attestation_silent_after_clean_ledger_exit(attest_home):
    """A clean lifecycle-ledger exit for the attested PID is a planned stop."""
    gateway_windows._write_start_attestation([777], "direct spawn (PID 777)")
    state = attest_home / "state"
    state.mkdir(exist_ok=True)
    (state / "gateway.lifecycle.json").write_text(
        json.dumps({"phase": "exited", "pid": 777, "exit_reason": "graceful_shutdown"}),
        encoding="utf-8",
    )
    assert gateway_windows.check_start_attestation(current_pids=[]) is None


def test_attestation_warning_includes_schtasks_recovery(monkeypatch, attest_home):
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
    monkeypatch.setattr(
        gateway_windows, "get_task_name", lambda: "Hermes_Gateway_arthur_tutor"
    )
    gateway_windows._write_start_attestation([888], "direct spawn (PID 888)")
    warning = gateway_windows.check_start_attestation(current_pids=[])
    assert "schtasks /Run /TN Hermes_Gateway_arthur_tutor" in warning


def test_attestation_tolerates_missing_and_garbage_marker(attest_home):
    assert gateway_windows.check_start_attestation(current_pids=[]) is None
    marker = attest_home / "state" / "gateway.start-attestation.json"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text("not json", encoding="utf-8")
    assert gateway_windows.check_start_attestation(current_pids=[]) is None
    marker.write_text(json.dumps({"pids": []}), encoding="utf-8")
    assert gateway_windows.check_start_attestation(current_pids=[]) is None
    assert not marker.exists()


def test_breakaway_fallback_warns_even_on_success(monkeypatch, attest_home, capsys):
    """When the spawn fell back to no-breakaway, the ✓ carries a Job warning."""
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda *a, **k: [99]
    )
    monkeypatch.setattr(gateway_windows, "is_task_registered", lambda: True)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway")
    gateway_windows._LAST_SPAWN_BREAKAWAY_FALLBACK["fallback"] = True
    try:
        gateway_windows._report_gateway_start("direct spawn (PID 99)")
    finally:
        gateway_windows._LAST_SPAWN_BREAKAWAY_FALLBACK["fallback"] = False
    out = capsys.readouterr().out
    assert "✓" in out
    assert "could not break away" in out
    assert "schtasks /Run /TN Hermes_Gateway" in out


# ---------------------------------------------------------------------------
# #109538: Attested gateway death probe, freshness, PID reuse & identity
# ---------------------------------------------------------------------------


def test_attestation_records_create_time_and_timestamp(monkeypatch, attest_home):
    import gateway.status as gst
    monkeypatch.setattr(gst, "get_process_start_time", lambda pid: 12345.67 if pid == 777 else None)
    gateway_windows._write_start_attestation([777], "direct spawn")
    marker = attest_home / "state" / "gateway.start-attestation.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data.get("pids") == [777]
    assert data.get("create_times") == {"777": 12345.67}
    assert isinstance(data.get("timestamp"), (int, float))
    assert isinstance(data.get("ts"), str)


def test_attested_probe_is_read_only_and_never_consumes_marker(attest_home):
    """The update path needs the death verdict without consuming the one-shot marker the
    next CLI start still owes the user; and only a *detectable* death may read True.

    ``hermes update`` consults this probe to keep the cold-start plan when Desktop owns
    the lifecycle (#109538) — consuming the marker here would silence the CLI-start
    warning that reports the same death to the user.
    """
    assert gateway_windows.attested_gateway_died(current_pids=[]) is False  # no marker yet

    gateway_windows._write_start_attestation([555], "cold-start after update")

    assert gateway_windows.attested_gateway_died(current_pids=[555]) is False  # alive
    assert gateway_windows.attested_gateway_died(current_pids=[]) is True  # dead, unclean
    marker = attest_home / "state" / "gateway.start-attestation.json"
    assert marker.exists()  # unconsumed — the CLI start below still reports it
    assert gateway_windows.check_start_attestation(current_pids=[]) is not None

    state = attest_home / "state"
    state.mkdir(exist_ok=True)
    (state / "gateway.lifecycle.json").write_text(
        json.dumps({"phase": "exited", "pid": 556, "exit_reason": "graceful_shutdown"}),
        encoding="utf-8",
    )
    gateway_windows._write_start_attestation([556], "cold-start after update")
    assert gateway_windows.attested_gateway_died(current_pids=[]) is False  # planned stop


def test_attested_probe_enforces_freshness_window(attest_home):
    """A stale crash marker from days ago must not authorize a competing gateway
    when Desktop currently owns lifecycle. Freshness window must be enforced."""
    state = attest_home / "state"
    state.mkdir(exist_ok=True)
    marker = state / "gateway.start-attestation.json"

    # Stale marker: timestamp from 2 days ago (172800s ago)
    two_days_ago = time.time() - 172800.0
    stale_payload = {
        "pids": [999],
        "create_times": {"999": 1000.0},
        "via": "test",
        "timestamp": two_days_ago,
        "ts": "2026-09-10T00:00:00+00:00",
    }
    marker.write_text(json.dumps(stale_payload), encoding="utf-8")
    assert gateway_windows.attested_gateway_died(current_pids=[], max_age_seconds=86400.0) is False

    # Fresh marker: timestamp from 10s ago
    fresh_payload = {
        "pids": [999],
        "create_times": {"999": 1000.0},
        "via": "test",
        "timestamp": time.time() - 10.0,
        "ts": "2026-09-13T00:00:00+00:00",
    }
    marker.write_text(json.dumps(fresh_payload), encoding="utf-8")
    assert gateway_windows.attested_gateway_died(current_pids=[], max_age_seconds=86400.0) is True


def test_attested_probe_pid_reuse_and_identity_validation(monkeypatch, attest_home):
    """PID reuse validation:
    1) If a running process shares the PID but has a different create_time, it is PID reuse.
    2) If an unrelated clean exit ledger exists with a different start_time, it must not suppress recovery."""
    import gateway.status as gst

    state = attest_home / "state"
    state.mkdir(exist_ok=True)
    marker = state / "gateway.start-attestation.json"

    payload = {
        "pids": [888],
        "create_times": {"888": 5000.0},
        "via": "test",
        "timestamp": time.time(),
        "ts": "2026-09-13T00:00:00+00:00",
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")

    # Running process has PID 888, but its create_time is 9000.0 (PID reused by another process!)
    monkeypatch.setattr(gst, "get_process_start_time", lambda pid: 9000.0 if pid == 888 else None)
    # Even though 888 is in current_pids, it's not the attested process!
    assert gateway_windows.attested_gateway_died(current_pids=[888]) is True

    # Same PID with matching create_time (5000.0) is the genuine live process
    monkeypatch.setattr(gst, "get_process_start_time", lambda pid: 5000.0 if pid == 888 else None)
    assert gateway_windows.attested_gateway_died(current_pids=[888]) is False

    # Unrelated clean exit in ledger with different start_time (2000.0 != 5000.0) does NOT suppress recovery
    (state / "gateway.lifecycle.json").write_text(
        json.dumps({"phase": "exited", "pid": 888, "start_time": 2000.0, "exit_reason": "graceful_shutdown"}),
        encoding="utf-8",
    )
    assert gateway_windows.attested_gateway_died(current_pids=[]) is True

    # Matching clean exit in ledger (start_time 5000.0 == 5000.0) proves legitimate clean exit
    (state / "gateway.lifecycle.json").write_text(
        json.dumps({"phase": "exited", "pid": 888, "start_time": 5000.0, "exit_reason": "graceful_shutdown"}),
        encoding="utf-8",
    )
    assert gateway_windows.attested_gateway_died(current_pids=[]) is False


def test_attested_probe_malformed_payload_fails_closed(attest_home):
    """Malformed payloads must fail closed safely: 'unknown must never read as dead'."""
    state = attest_home / "state"
    state.mkdir(exist_ok=True)
    marker = state / "gateway.start-attestation.json"

    for bad in [
        "not json",
        json.dumps(None),
        json.dumps("string"),
        json.dumps([]),
        json.dumps({}),
        json.dumps({"pids": None}),
        json.dumps({"pids": "not_a_list"}),
        json.dumps({"pids": [None]}),
        json.dumps({"pids": ["not_int"]}),
        json.dumps({"pids": [123], "timestamp": "bad_ts", "ts": None}),
    ]:
        marker.write_text(bad, encoding="utf-8")
        assert gateway_windows.attested_gateway_died(current_pids=[]) is False


def test_attested_probe_clock_skew_and_start_time_units():
    """Verify clock-skew tolerance and process start-time comparison across units."""
    # Clock skew: minor negative age (-10s, NTP step) tolerated; future time (-70s) rejected
    assert gateway_windows._is_attestation_fresh({"timestamp": time.time() + 10.0}) is True
    assert gateway_windows._is_attestation_fresh({"timestamp": time.time() + 70.0}) is False

    # Start times matching: None matches as wildcard
    assert gateway_windows._start_times_match(None, 12345.0) is True
    assert gateway_windows._start_times_match(12345.0, None) is True

    # Seconds unit (e.g. float epoch ~ 1.7e9): within 2.0s is match
    assert gateway_windows._start_times_match(1726000000.0, 1726000001.5) is True
    assert gateway_windows._start_times_match(1726000000.0, 1726000005.0) is False

    # Centiseconds unit (Windows psutil: ~ 1.7e11): within 200 centiseconds is match
    assert gateway_windows._start_times_match(172600000000, 172600000150) is True
    assert gateway_windows._start_times_match(172600000000, 172600000500) is False

    # Non-numeric gracefully handled
    assert gateway_windows._start_times_match("invalid", 123) is False


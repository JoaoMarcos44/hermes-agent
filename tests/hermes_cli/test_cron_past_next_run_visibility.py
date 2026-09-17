"""Past next_run_at must not read as an upcoming schedule slot (#114309).

When the scheduler host stops ticking, jobs.json freezes next_run_at in the past.
status/list used to print that stamp as \"Next run\", which hid the outage behind
ordinary schedule text. Doctor already flagged the same stamp past a 15-minute
grace; the display path must share that threshold, order mixed-offset stamps by
instant (not ISO text), and keep store bytes unchanged.
"""

from __future__ import annotations

import time
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import pytest

from cron import jobs
from hermes_cli import cron
from hermes_time import now as hermes_now


@pytest.fixture()
def isolated_cron(tmp_path, monkeypatch):
    """Real profile-local cron store under a temp home; no live gateway."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cron, "_builtin_gateway_liveness", lambda: False)
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr(
        "hermes_cli.gateway.named_profile_served_by_running_multiplexer", lambda: False
    )
    monkeypatch.setattr("gateway.status.is_gateway_runtime_lock_active", lambda: False)
    with jobs.use_cron_store(home):
        yield home


def _stamp_first_next_run(iso: str) -> str:
    records = jobs.load_jobs()
    assert records, "expected a job to stamp"
    records[0]["next_run_at"] = iso
    jobs.save_jobs(records)
    return iso


@pytest.mark.parametrize("surface", ["status", "list"])
def test_seven_hour_past_slot_is_overdue_not_next_run(isolated_cron, capsys, surface):
    jobs.create_job(prompt="Hourly report", schedule="every 60m")
    scheduled = (hermes_now() - timedelta(hours=7)).isoformat()
    _stamp_first_next_run(scheduled)
    store = jobs._current_cron_store().jobs_file
    before = store.read_bytes()

    assert cron.cron_command(Namespace(cron_command=surface, all=False)) == 0
    out = capsys.readouterr().out

    assert "Overdue since" in out
    assert scheduled in out
    assert "Next run:" not in out
    assert store.read_bytes() == before


@pytest.mark.parametrize("surface", ["status", "list"])
def test_future_slot_stays_next_run(isolated_cron, capsys, surface):
    jobs.create_job(prompt="Hourly report", schedule="every 60m")
    scheduled = (hermes_now() + timedelta(hours=2)).isoformat()
    _stamp_first_next_run(scheduled)

    assert cron.cron_command(Namespace(cron_command=surface, all=False)) == 0
    out = capsys.readouterr().out

    assert "Next run:" in out
    assert scheduled in out
    assert "Overdue since" not in out


@pytest.mark.parametrize("surface", ["status", "list"])
def test_within_doctor_grace_stays_next_run(isolated_cron, capsys, surface):
    """status/list must agree with doctor: 5 minutes late is still healthy tick lag."""
    jobs.create_job(prompt="Hourly report", schedule="every 60m")
    scheduled = (hermes_now() - timedelta(minutes=5)).isoformat()
    _stamp_first_next_run(scheduled)

    assert cron.cron_command(Namespace(cron_command=surface, all=False)) == 0
    out = capsys.readouterr().out

    assert "Next run:" in out
    assert "Overdue since" not in out
    assert cron.cron_command(Namespace(cron_command="doctor")) == 0
    assert "✓ Cron doctor found no issues" in capsys.readouterr().out


def test_status_selects_earliest_instant_not_lexicographic_iso(
    isolated_cron, capsys, monkeypatch
):
    """Mixed UTC offsets: text min() would hide the real overdue job."""
    early = jobs.create_job(prompt="east", schedule="every 60m", name="east")
    late = jobs.create_job(prompt="west", schedule="every 60m", name="west")
    # early_instant = 17:57Z (overdue); late_instant = 19:57Z (future) at frozen now 18:30Z.
    overdue_stamp = "2026-09-18T07:57:47+14:00"  # 2026-09-17T17:57:47Z
    future_stamp = "2026-09-17T07:57:47-12:00"  # 2026-09-17T19:57:47Z
    frozen = datetime(2026, 9, 17, 18, 30, tzinfo=timezone.utc)

    records = {j["id"]: j for j in jobs.load_jobs()}
    records[early["id"]]["next_run_at"] = overdue_stamp
    records[late["id"]]["next_run_at"] = future_stamp
    jobs.save_jobs(list(records.values()))

    monkeypatch.setattr("hermes_time.now", lambda: frozen)

    assert cron.cron_command(Namespace(cron_command="status", all=False)) == 0
    out = capsys.readouterr().out

    assert "Overdue since" in out
    assert overdue_stamp in out
    assert f"Next run: {future_stamp}" not in out
    # Lexicographic min of the two ISO strings is the future stamp — must not win.
    assert min([overdue_stamp, future_stamp]) == future_stamp


def test_dead_gateway_surfaces_stale_ticker_heartbeat(isolated_cron, capsys):
    jobs.create_job(prompt="Hourly report", schedule="every 60m")
    scheduled = (hermes_now() - timedelta(hours=7)).isoformat()
    _stamp_first_next_run(scheduled)
    hb = jobs._current_cron_store().cron_dir / "ticker_heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(str(time.time() - 25 * 3600), encoding="utf-8")

    assert cron.cron_command(Namespace(cron_command="status", all=False)) == 0
    out = capsys.readouterr().out

    assert "Gateway is not running" in out
    assert "Last ticker heartbeat" in out
    assert "Overdue since" in out
    assert "Next run:" not in out


def test_paused_job_past_slot_is_not_labelled_overdue(isolated_cron, capsys):
    job = jobs.create_job(prompt="Paused report", schedule="every 60m")
    records = jobs.load_jobs()
    for row in records:
        if row["id"] == job["id"]:
            row["state"] = "paused"
            row["enabled"] = False
            row["next_run_at"] = (hermes_now() - timedelta(hours=7)).isoformat()
    jobs.save_jobs(records)

    assert cron.cron_command(Namespace(cron_command="list", all=True)) == 0
    out = capsys.readouterr().out
    assert "Overdue since" not in out
    assert "Next run:" in out


def test_earliest_helper_skips_unparseable_and_orders_by_instant():
    stamps = [
        "not-a-date",
        "2026-09-18T04:56:41+14:00",  # 14:56Z
        "2026-09-17T12:56:41-06:00",  # 18:56Z
    ]
    assert cron._earliest_next_run_stamp(stamps) == "2026-09-18T04:56:41+14:00"
    assert cron._earliest_next_run_stamp(["bogus", None, ""]) is None

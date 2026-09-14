"""#76129: post-update Windows cold-start must not steal Desktop-owned lifecycle.

A vestigial Startup/Scheduled-Task autostart is not proof the user wants a
standalone ``gateway run``. When Desktop currently supervises this install's
control plane, the updater must not spawn a competing messaging daemon.

Serve/dashboard are the control plane, not the messaging gateway (#92091).
``looks_like_gateway_command_line`` stays strict; ownership is a separate
predicate.
"""

from __future__ import annotations

import json

from hermes_cli import gateway as hermes_gateway
from hermes_cli import gateway_windows
from hermes_cli import main as cli_main
import hermes_cli.main_install_repair as main_install_repair
from hermes_cli import process_identity
from hermes_cli import update_cmd
import hermes_cli.update_cmd_windows as update_cmd_windows


def _live_serve_ledger_entry() -> dict:
    return {
        "pid": 111,
        "create_time": 1.0,
        "purpose": "serve",
        "install": "abc",
        "spawner_pid": 99,
        "spawner_create": 0.5,
    }


def test_control_plane_argv_is_not_a_gateway():
    from gateway.status import looks_like_gateway_command_line

    serve = "C:\\Hermes\\.venv\\Scripts\\python.exe -m hermes_cli.main serve --host 127.0.0.1"
    run = "C:\\Hermes\\.venv\\Scripts\\python.exe -m hermes_cli.main gateway run"

    assert update_cmd._looks_like_desktop_control_plane(serve) is True
    assert looks_like_gateway_command_line(serve) is False
    assert update_cmd._looks_like_desktop_control_plane(run) is False
    assert looks_like_gateway_command_line(run) is True


def test_control_plane_classifier_is_token_based_not_substring():
    """#90778/#91869 class: flag values and lookalike tokens must not read
    as a control plane. The salvage swapped the original substring check
    for the parser-derived subcommand classifier."""
    py = "C:\\Hermes\\.venv\\Scripts\\python.exe -m hermes_cli.main"
    # "dashboard" as a FLAG VALUE, real subcommand is chat
    assert update_cmd._looks_like_desktop_control_plane(f"{py} -m dashboard chat") is False
    # "--preserve-cache" contains "serve"; real subcommand is kanban
    assert (
        update_cmd._looks_like_desktop_control_plane(f"{py} kanban --preserve-cache")
        is False
    )
    # profile selector before the real subcommand still classifies correctly
    assert (
        update_cmd._looks_like_desktop_control_plane(f"{py} --profile serve dashboard")
        is True
    )
    # dashboard as the real subcommand
    assert update_cmd._looks_like_desktop_control_plane(f"{py} dashboard") is True
    # undeterminable subcommand → NOT a control plane (never guess ownership)
    assert update_cmd._looks_like_desktop_control_plane("python.exe -c import time") is False


def test_ledger_live_serve_with_live_spawner_owns_lifecycle(monkeypatch):
    monkeypatch.setattr(
        process_identity, "ledger_entries", lambda **_k: [_live_serve_ledger_entry()]
    )
    monkeypatch.setattr(process_identity, "spawner_is_dead", lambda _e: False)
    monkeypatch.setattr(cli_main, "_detect_venv_python_processes", lambda: [])

    assert update_cmd._desktop_owns_gateway_lifecycle() is True


def test_orphaned_control_plane_does_not_own_lifecycle(monkeypatch):
    monkeypatch.setattr(
        process_identity, "ledger_entries", lambda **_k: [_live_serve_ledger_entry()]
    )
    monkeypatch.setattr(process_identity, "spawner_is_dead", lambda _e: True)
    monkeypatch.setattr(cli_main, "_detect_venv_python_processes", lambda: [])

    assert update_cmd._desktop_owns_gateway_lifecycle() is False


def test_pause_skips_cold_start_plan_when_desktop_owns_lifecycle(monkeypatch):
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: True)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda **_k: [])
    monkeypatch.setattr(
        hermes_gateway, "find_windows_gateway_services", lambda **_k: []
    )
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: True)
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: True)
    monkeypatch.setattr(update_cmd_windows, "_desktop_owns_gateway_lifecycle", lambda: True)

    assert update_cmd._pause_windows_gateways_for_update() is None


def test_pause_still_cold_starts_when_autostart_and_no_desktop_owner(monkeypatch):
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: True)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda **_k: [])
    monkeypatch.setattr(
        hermes_gateway, "find_windows_gateway_services", lambda **_k: []
    )
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: True)
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: False)
    monkeypatch.setattr(update_cmd_windows, "_desktop_owns_gateway_lifecycle", lambda: False)

    token = update_cmd._pause_windows_gateways_for_update()

    assert token == {
        "resume_needed": True,
        "profiles": {},
        "unmapped_pids": [],
        "unmapped": [],
        "cold_start_if_installed": True,
    }


def test_cold_start_aborts_when_desktop_owns_lifecycle(monkeypatch):
    spawned = []
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: True)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda **_k: [])
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: True)
    monkeypatch.setattr(update_cmd_windows, "_desktop_owns_gateway_lifecycle", lambda: True)
    monkeypatch.setattr(
        gateway_windows, "_spawn_detached", lambda: spawned.append(1) or 4242
    )

    update_cmd._cold_start_windows_gateway_after_update()

    assert spawned == []


# ---------------------------------------------------------------------------
# #110469: Handoff recovery vs Stale Marker refusal under Desktop ownership
# ---------------------------------------------------------------------------


def test_pause_skips_cold_start_plan_for_stale_attestation_when_desktop_owns_lifecycle(
    monkeypatch, tmp_path
):
    """A stale marker (e.g. 13 days old) must NOT authorize a cold start (#110469)."""
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: True)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda **_k: [])
    monkeypatch.setattr(hermes_gateway, "find_windows_gateway_services", lambda **_k: [])
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: True)
    monkeypatch.setattr(gateway_windows, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: True)
    monkeypatch.setattr(update_cmd_windows, "_desktop_owns_gateway_lifecycle", lambda: True)

    # Write a 13-day-old attestation without any matching hand-off
    marker = tmp_path / "state" / "gateway.start-attestation.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({
            "pids": [1234],
            "create_times": {"1234": 100},
            "via": "direct spawn",
            "ts": "2026-09-01T00:00:00Z",
        }),
        encoding="utf-8",
    )

    # Must return None — Desktop ownership refusal preserved
    assert update_cmd._pause_windows_gateways_for_update() is None


def test_pause_keeps_cold_start_plan_for_attested_handoff_death_when_desktop_owns_lifecycle(
    monkeypatch, tmp_path
):
    """Attested gateway killed during update handoff preserves cold-start plan (#110469, #109538)."""
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: True)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda **_k: [])
    monkeypatch.setattr(hermes_gateway, "find_windows_gateway_services", lambda **_k: [])
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: True)
    monkeypatch.setattr(gateway_windows, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: True)
    monkeypatch.setattr(update_cmd_windows, "_desktop_owns_gateway_lifecycle", lambda: True)

    # Attestation recorded for a gateway (could have run for days)
    marker = tmp_path / "state" / "gateway.start-attestation.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({
            "pids": [4321],
            "create_times": {"4321": 555555},
            "via": "direct spawn",
            "ts": "2026-08-01T00:00:00Z",
        }),
        encoding="utf-8",
    )

    # Hand-off recorded right before Desktop exited/updated
    handoff = tmp_path / "state" / "gateway.update-handoff.json"
    handoff.write_text(
        json.dumps({
            "nonce": "handoff-nonce-xyz",
            "pids": [4321],
            "create_times": {"4321": 555555},
            "recorded_at": "2026-09-13T22:00:00Z",
        }),
        encoding="utf-8",
    )

    token = update_cmd._pause_windows_gateways_for_update()
    assert token is not None
    assert token["cold_start_if_installed"] is True
    assert "cold_start_verified_identity" in token
    assert token["cold_start_verified_identity"]["pids"] == [4321]
    assert token["cold_start_verified_identity"]["nonce"] == "handoff-nonce-xyz"


def test_cold_start_restores_attested_handoff_gateway_and_consumes_markers(
    monkeypatch, tmp_path
):
    """Cold-start spawn executes for verified handoff identity and consumes markers (#110469)."""
    spawned = []
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: True)
    monkeypatch.setattr(hermes_gateway, "find_gateway_pids", lambda **_k: [])
    monkeypatch.setattr(gateway_windows, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: True)
    monkeypatch.setattr(update_cmd_windows, "_desktop_owns_gateway_lifecycle", lambda: True)
    monkeypatch.setattr(
        gateway_windows, "_spawn_detached", lambda: spawned.append(1) or 9999
    )
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda: [9999]
    )

    handoff = tmp_path / "state" / "gateway.update-handoff.json"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(json.dumps({"nonce": "xyz", "pids": [4321], "create_times": {"4321": 100}}), encoding="utf-8")

    token = {
        "cold_start_if_installed": True,
        "cold_start_verified_identity": {"pids": [4321], "create_times": {"4321": 100}, "nonce": "xyz"},
    }

    assert update_cmd._cold_start_windows_gateway_after_update(token) is True
    assert spawned == [1]
    # Hand-off marker consumed on spawn
    assert not handoff.exists()


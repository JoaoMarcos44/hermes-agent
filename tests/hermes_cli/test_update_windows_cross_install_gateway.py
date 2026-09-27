"""Windows update gateway ownership is install-scoped, not machine-scoped (#124659)."""

from types import SimpleNamespace

from hermes_cli import gateway as gateway_mod
from hermes_cli import gateway_windows
from hermes_cli import main as cli_main
import hermes_cli.main_install_repair as main_install_repair
from hermes_cli import process_identity
from hermes_cli import update_cmd
from hermes_cli import update_cmd_windows


def test_current_install_filter_keeps_ledger_owned_unmapped_gateway(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        process_identity, "ledger_entries", lambda **_kwargs: [{"pid": 101, "purpose": "gateway"}]
    )
    monkeypatch.setattr(update_cmd_windows, "_psutil", lambda: None)

    assert update_cmd_windows._current_install_gateway_pids([101, 202]) == [101]


def test_legacy_home_proves_owned_unmapped_gateway(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    home = tmp_path / "hermes"
    foreign = tmp_path / "foreign"
    for path in (root, home, foreign):
        path.mkdir()
    profile_home = home / "profiles" / "work"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(process_identity, "ledger_entries", lambda **_kwargs: [])

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def environ(self):
            return {"HERMES_HOME": str(profile_home if self.pid == 101 else foreign)}

        def exe(self):
            return str(foreign / "python.exe")

        def cmdline(self):
            return [str(foreign / "python.exe")]

    monkeypatch.setattr(update_cmd_windows, "_psutil", lambda: SimpleNamespace(Process=Proc))

    owned, unknown = update_cmd_windows._classify_current_install_gateway_pids([101, 202])
    assert owned == [101]
    assert unknown == []


def test_cwd_alone_is_not_destructive_ownership(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    home = tmp_path / "hermes"
    foreign = tmp_path / "foreign"
    for path in (root, home, foreign):
        path.mkdir()
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(process_identity, "ledger_entries", lambda **_kwargs: [])

    proc = SimpleNamespace(
        environ=lambda: {},
        exe=lambda: str(foreign / "python.exe"),
        cmdline=lambda: [str(foreign / "python.exe")],
        cwd=lambda: str(root),
    )
    monkeypatch.setattr(
        update_cmd_windows, "_psutil", lambda: SimpleNamespace(Process=lambda _pid: proc)
    )

    owned, unknown = update_cmd_windows._classify_current_install_gateway_pids([202])
    assert owned == []
    assert unknown == [202]


def test_discovery_keeps_owned_unmapped_and_excludes_foreign(monkeypatch):
    monkeypatch.setattr(gateway_mod, "find_profile_gateway_processes", lambda **_kwargs: [])
    monkeypatch.setattr(gateway_mod, "find_windows_gateway_services", lambda **_kwargs: [])
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_kwargs: [101, 202])
    monkeypatch.setattr(
        update_cmd_windows,
        "_current_install_gateway_pids",
        lambda pids, **_kwargs: [pid for pid in pids if pid == 101],
    )

    profiles, services, service_pids, running = update_cmd_windows._discover_windows_gateways()

    assert profiles == {}
    assert services == []
    assert service_pids == set()
    assert running == [101]


def test_foreign_gateway_does_not_suppress_current_install_cold_start(monkeypatch):
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: True)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_kwargs: [202])
    monkeypatch.setattr(
        update_cmd_windows,
        "_classify_current_install_gateway_pids",
        lambda _pids, **_kwargs: ([], []),
    )
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: False)

    spawned = []
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda: spawned.append(True) or 4242)
    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", lambda *a, **k: [4242])
    monkeypatch.setattr(gateway_windows, "_write_start_attestation", lambda *a, **k: None)

    assert update_cmd_windows._cold_start_windows_gateway_after_update({"attested_generation": None})
    assert spawned == [True]


def test_unknown_gateway_conservatively_suppresses_cold_start(monkeypatch):
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(main_install_repair, "_is_windows", lambda: True)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_kwargs: [303])
    monkeypatch.setattr(
        update_cmd_windows,
        "_classify_current_install_gateway_pids",
        lambda _pids, **_kwargs: ([], [303]),
    )
    monkeypatch.setattr(update_cmd, "_desktop_owns_gateway_lifecycle", lambda: False)
    monkeypatch.setattr(
        gateway_windows,
        "_spawn_detached",
        lambda: (_ for _ in ()).throw(AssertionError("must not spawn with unknown ownership")),
    )

    assert update_cmd_windows._cold_start_windows_gateway_after_update(
        {"attested_generation": None}
    )


def test_readiness_filter_waits_past_foreign_gateway(monkeypatch):
    snapshots = iter(([202], [202, 4242]))
    monkeypatch.setattr(
        gateway_mod, "find_gateway_pids", lambda **_kwargs: next(snapshots, [202, 4242])
    )
    monkeypatch.setattr(gateway_windows.time, "sleep", lambda _seconds: None)

    ready = gateway_windows._wait_for_gateway_ready(
        timeout_s=1.0,
        confirm_s=0,
        all_profiles=True,
        pid_filter=lambda pids: [pid for pid in pids if pid == 4242],
    )

    assert ready == [4242]


def test_post_relaunch_verification_uses_install_filter(monkeypatch):
    captured = {}

    def wait_for_ready(**kwargs):
        captured.update(kwargs)
        return [4242]

    monkeypatch.setattr(gateway_windows, "_wait_for_gateway_ready", wait_for_ready)
    monkeypatch.setattr(gateway_windows, "_write_start_attestation", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd_windows, "_relaunch_verify_timeout_s", lambda *_args: 1.0)

    update_cmd_windows._verify_relaunched_gateways_alive({}, {}, [])

    assert captured["all_profiles"] is True
    assert captured["pid_filter"] is update_cmd_windows._current_install_gateway_pids

"""Dry-run uninstall regressions, including GUI safety from #128974."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import main, uninstall


def test_dry_run_prints_plan_without_mutating(monkeypatch, tmp_path, capsys):
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    # A .git dir marks the tree as a removable git checkout — without it the
    # install-kind gate refuses before the dry-run plan prints.
    (project_root / ".git").mkdir()
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model: {}\n", encoding="utf-8")

    called = False

    def _fail_if_called(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(uninstall, "get_project_root", lambda: project_root)
    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda home: False)
    monkeypatch.setattr(uninstall, "_discover_named_profiles", lambda: [])
    monkeypatch.setattr(uninstall, "_perform_uninstall", _fail_if_called)

    uninstall.run_uninstall(SimpleNamespace(dry_run=True, yes=True, full=True))

    output = capsys.readouterr().out
    assert called is False
    assert "Dry run" in output
    assert str(project_root) in output
    assert str(hermes_home) in output
    assert project_root.exists()
    assert hermes_home.exists()


def test_dry_run_lists_named_profiles_without_desktop_userdata(monkeypatch, tmp_path, capsys):
    """Full-uninstall dry-run lists named profiles even on a machine with no desktop
    userData dir — the profiles section must not depend on the desktop install."""
    profile = SimpleNamespace(name="work", path=tmp_path / "profiles" / "work")
    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda home: True)
    monkeypatch.setattr(uninstall, "_discover_named_profiles", lambda: [profile])
    monkeypatch.setattr(
        "hermes_cli.gui_uninstall.desktop_userdata_dir", lambda: tmp_path / "absent-userdata"
    )

    uninstall._print_uninstall_dry_run(
        project_root=tmp_path, hermes_home=tmp_path / ".hermes", full_uninstall=True
    )

    out = capsys.readouterr().out
    assert "Named profiles" in out
    assert "work" in out


def test_build_uninstall_parser_accepts_dry_run():
    import argparse
    from hermes_cli.subcommands.uninstall import build_uninstall_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_uninstall_parser(subparsers, cmd_uninstall=lambda args: args)

    args = parser.parse_args(["uninstall", "--dry-run", "--full"])

    assert args.dry_run is True
    assert args.full is True


@pytest.mark.parametrize("yes", [False, True])
def test_gui_dry_run_never_prompts_or_mutates(monkeypatch, tmp_path, capsys, yes):
    """Regression for #128974: --gui --dry-run is read-only for every confirmation mode."""
    hermes_home = tmp_path / ".hermes"
    agent_root = hermes_home / "hermes-agent"
    (agent_root / "hermes_cli").mkdir(parents=True)
    desktop_dist = agent_root / "apps" / "desktop" / "dist"
    desktop_dist.mkdir(parents=True)
    shared_dep = agent_root / "node_modules" / "web" / "package.json"
    shared_dep.parent.mkdir(parents=True)
    shared_dep.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(uninstall, "_refuse_if_steward_owned", lambda: None)
    monkeypatch.setattr("hermes_cli.gui_uninstall.packaged_gui_app_paths", lambda: [])
    monkeypatch.setattr("hermes_cli.gui_uninstall.desktop_userdata_dir", lambda: tmp_path / "none")

    def _unexpected_prompt(*_args, **_kwargs):
        pytest.fail("GUI dry-run must not prompt")

    monkeypatch.setattr(uninstall, "_confirm_yes", _unexpected_prompt)

    uninstall.run_gui_uninstall(SimpleNamespace(dry_run=True, yes=yes))

    out = capsys.readouterr().out
    assert "Dry run: no files or processes changed." in out
    assert str(desktop_dist) in out
    assert str(agent_root / "node_modules") not in out
    assert desktop_dist.exists()
    assert shared_dep.exists()


def test_gui_dry_run_bypasses_tty_gate(monkeypatch):
    """A read-only dry-run is valid from scripts/CI just like --data --dry-run."""
    dispatched = []

    def _unexpected_tty(*_args, **_kwargs):
        pytest.fail("GUI dry-run must not require a TTY")

    monkeypatch.setattr(main, "_require_tty", _unexpected_tty)
    monkeypatch.setattr(uninstall, "run_gui_uninstall", lambda args: dispatched.append(args))
    args = SimpleNamespace(
        gui_summary=False,
        data=False,
        gui=True,
        yes=False,
        dry_run=True,
    )

    main.cmd_uninstall(args)

    assert dispatched == [args]

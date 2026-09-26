"""Real-filesystem regression coverage for Desktop userData policy during agent uninstall."""

from contextlib import contextmanager
import os
import sys
from pathlib import Path

import pytest

import hermes_cli.uninstall as uninstall
from hermes_cli.gui_uninstall import (
    desktop_userdata_dir,
    packaged_gui_app_paths,
    source_built_gui_artifacts,
)


@contextmanager
def _isolated_uninstall_environment(root: Path):
    """Route every user-scoped Desktop/Hermes path into a real temporary filesystem tree."""
    user_home = root / "user-home"
    hermes_home = root / "hermes-home"
    user_home.mkdir()
    hermes_home.mkdir()

    isolated = {
        "HOME": str(user_home),
        "USERPROFILE": str(user_home),
        "HERMES_HOME": str(hermes_home),
        "APPDATA": str(root / "appdata"),
        "LOCALAPPDATA": str(root / "localappdata"),
        "ProgramFiles": str(root / "program-files"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
    }
    previous = {name: os.environ.get(name) for name in isolated}
    previous_suffix = os.environ.pop("HERMES_DATA_DIR_SUFFIX", None)
    os.environ.update(isolated)
    try:
        yield hermes_home
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if previous_suffix is not None:
            os.environ["HERMES_DATA_DIR_SUFFIX"] = previous_suffix


def _write_real_desktop_state(root: Path, hermes_home: Path) -> tuple[Path, Path, Path]:
    built = source_built_gui_artifacts(hermes_home)[0]
    built.mkdir(parents=True)
    (built / "renderer.js").write_text("console.log('real artifact')", encoding="utf-8")

    userdata = desktop_userdata_dir()
    userdata.mkdir(parents=True)
    connections = userdata / "connections.json"
    connections.write_text(
        '{"connections":[{"id":"remote-prod","host":"gateway.example.test"}]}',
        encoding="utf-8",
    )

    safe_packaged = [path for path in packaged_gui_app_paths() if path == root or root in path.parents]
    assert safe_packaged, "isolated environment must keep packaged Desktop paths under tmp_path"
    packaged = safe_packaged[-1]
    packaged.parent.mkdir(parents=True, exist_ok=True)
    if packaged.suffix:
        packaged.write_text("real packaged artifact", encoding="utf-8")
    else:
        packaged.mkdir(parents=True, exist_ok=True)
        (packaged / "artifact.txt").write_text("real packaged artifact", encoding="utf-8")

    return built, userdata, packaged


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="macOS discovery includes /Applications/Hermes.app; a real-filesystem test must never touch a host app",
)
def test_agent_uninstall_desktop_policy_uses_real_filesystem(tmp_path):
    with _isolated_uninstall_environment(tmp_path) as hermes_home:
        built, userdata, packaged = _write_real_desktop_state(tmp_path, hermes_home)
        connections = userdata / "connections.json"
        expected_connections = connections.read_text(encoding="utf-8")

        removed = uninstall._uninstall_desktop_artifacts(
            hermes_home,
            full_uninstall=False,
        )

        assert not built.exists()
        assert not packaged.exists()
        assert userdata.exists()
        assert connections.read_text(encoding="utf-8") == expected_connections
        assert userdata not in removed

        removed = uninstall._uninstall_desktop_artifacts(
            hermes_home,
            full_uninstall=True,
        )

        assert not userdata.exists()
        assert userdata in removed


def test_full_dry_run_discovers_real_profile_without_desktop_userdata(tmp_path, capsys):
    with _isolated_uninstall_environment(tmp_path) as hermes_home:
        (hermes_home / "config.yaml").write_text("{}", encoding="utf-8")
        profile = hermes_home / "profiles" / "work"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}", encoding="utf-8")
        assert not desktop_userdata_dir().exists()

        uninstall._print_uninstall_dry_run(
            project_root=tmp_path / "checkout",
            hermes_home=hermes_home,
            full_uninstall=True,
        )

        output = capsys.readouterr().out
        assert "Named profiles" in output
        assert f"work: {profile}" in output


def test_keep_data_dry_run_uses_real_userdata_without_listing_real_profile(tmp_path, capsys):
    with _isolated_uninstall_environment(tmp_path) as hermes_home:
        (hermes_home / "config.yaml").write_text("{}", encoding="utf-8")
        profile = hermes_home / "profiles" / "work"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}", encoding="utf-8")

        userdata = desktop_userdata_dir()
        userdata.mkdir(parents=True)
        (userdata / "connections.json").write_text(
            '{"connections":[{"id":"remote-prod"}]}',
            encoding="utf-8",
        )

        uninstall._print_uninstall_dry_run(
            project_root=tmp_path / "checkout",
            hermes_home=hermes_home,
            full_uninstall=False,
        )

        output = capsys.readouterr().out
        assert f"Keep desktop app data: {userdata}" in output
        assert "Named profiles" not in output
        assert str(profile) not in output

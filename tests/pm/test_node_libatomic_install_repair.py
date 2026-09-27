"""Install-only libatomic recovery for the official Node runtime (#124926)."""

from pathlib import Path

import pytest

from pm.install import _verify_staged
from pm.packages import Nodejs, _node_libatomic_manager


_LOADER = (
    "bin/node: error while loading shared libraries: libatomic.so.1: "
    "cannot open shared object file: No such file or directory"
)


def _script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.platforms("posix")
def test_node_verify_stays_side_effect_free(tmp_path, monkeypatch):
    """Doctor/read validation must diagnose, never install a system package."""
    node = tmp_path / "bin" / "node"
    _script(node, f'#!/bin/sh\necho "{_LOADER}" >&2\nexit 127\n')
    monkeypatch.setattr("pm.packages.current_target", lambda: "linux-x64")

    def refuse():
        raise AssertionError("Nodejs.verify() must not attempt host repair")

    monkeypatch.setattr("pm.packages._try_install_node_libatomic", refuse)
    reason = Nodejs().verify(tmp_path, "linux-x64")

    assert "libatomic.so.1" in reason
    assert "exited 127" in reason


@pytest.mark.platforms("posix")
def test_staged_install_repairs_and_retries_once(tmp_path, monkeypatch):
    node = tmp_path / "bin" / "node"
    _script(node, f'#!/bin/sh\necho "{_LOADER}" >&2\nexit 127\n')
    monkeypatch.setattr("pm.packages.current_target", lambda: "linux-x64")
    repairs = {"n": 0}

    def install():
        repairs["n"] += 1
        _script(node, "#!/bin/sh\necho v26.7.0\n")
        return True, "unused"

    monkeypatch.setattr("pm.packages._try_install_node_libatomic", install)

    assert _verify_staged(Nodejs(), tmp_path, "linux-x64") == ""
    assert repairs["n"] == 1


@pytest.mark.platforms("posix")
def test_failed_staged_repair_names_almalinux_command(tmp_path, monkeypatch):
    node = tmp_path / "bin" / "node"
    _script(node, f'#!/bin/sh\necho "{_LOADER}" >&2\nexit 127\n')
    monkeypatch.setattr("pm.packages.current_target", lambda: "linux-x64")
    monkeypatch.setattr(
        "pm.packages._try_install_node_libatomic",
        lambda: (False, "install the missing Node runtime dependency with `sudo dnf install -y libatomic` "
                        "and rerun `hermes update`"),
    )

    reason = _verify_staged(Nodejs(), tmp_path, "linux-x64")

    assert "libatomic.so.1" in reason
    assert "sudo dnf install -y libatomic" in reason


@pytest.mark.platforms("posix")
def test_successful_libatomic_install_does_not_mask_a_new_probe_error(tmp_path, monkeypatch):
    node = tmp_path / "bin" / "node"
    _script(node, f'#!/bin/sh\necho "{_LOADER}" >&2\nexit 127\n')
    monkeypatch.setattr("pm.packages.current_target", lambda: "linux-x64")

    def install():
        _script(node, "#!/bin/sh\necho different-failure >&2\nexit 23\n")
        return True, "install with `sudo dnf install -y libatomic`"

    monkeypatch.setattr("pm.packages._try_install_node_libatomic", install)
    reason = _verify_staged(Nodejs(), tmp_path, "linux-x64")

    assert "different-failure" in reason
    assert "libatomic" not in reason


def test_almalinux_prefers_dnf_even_if_apt_is_present(monkeypatch):
    monkeypatch.setattr(
        "pm.packages.platform.freedesktop_os_release",
        lambda: {"ID": "almalinux", "ID_LIKE": "rhel centos fedora"},
    )
    monkeypatch.setattr(
        "pm.packages.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"apt-get", "dnf"} else None,
    )

    assert _node_libatomic_manager() == "dnf"


def test_known_family_never_falls_back_to_wrong_manager(monkeypatch):
    monkeypatch.setattr(
        "pm.packages.platform.freedesktop_os_release",
        lambda: {"ID": "almalinux", "ID_LIKE": "rhel centos fedora"},
    )
    monkeypatch.setattr(
        "pm.packages.shutil.which",
        lambda name: "/usr/bin/apt-get" if name == "apt-get" else None,
    )

    assert _node_libatomic_manager() == "dnf"


def test_cross_target_staging_never_repairs_host(monkeypatch):
    monkeypatch.setattr("pm.packages.current_target", lambda: "linux-x64")

    def refuse():
        raise AssertionError("cross-target verification must not mutate the host")

    monkeypatch.setattr("pm.packages._try_install_node_libatomic", refuse)
    reason = f"node --version exited 127: {_LOADER}"

    assert Nodejs().repair_staged_verification(
        Path("/tmp/not-used"), "linux-arm64", reason
    ) == reason

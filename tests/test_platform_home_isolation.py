"""Regression tests for test-home isolation from the platform-native Hermes home."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_constants


def _is_within(path: Path, parent: Path) -> bool:
    """Return whether *path* is *parent* or one of its descendants."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def test_test_home_stays_outside_platform_native_home():
    native_home = Path(hermes_constants._get_platform_default_hermes_home()).resolve()
    test_home = Path(os.environ["HERMES_HOME"]).resolve()

    assert not _is_within(test_home, native_home), (
        f"the test session home {test_home} is inside the platform-native home "
        f"{native_home}"
    )


def test_collection_home_stays_outside_platform_native_home():
    from tests.conftest import HERMES_HOME_AT_CONFTEST_IMPORT

    native_home = Path(hermes_constants._get_platform_default_hermes_home()).resolve()
    collection_home = Path(HERMES_HOME_AT_CONFTEST_IMPORT).resolve()

    assert not _is_within(collection_home, native_home), (
        f"the collection-time test home {collection_home} is inside the "
        f"platform-native home {native_home}"
    )


def test_pytest_temp_root_stays_outside_platform_native_home(tmp_path):
    native_home = Path(hermes_constants._get_platform_default_hermes_home()).resolve()

    assert not _is_within(tmp_path.resolve(), native_home), (
        f"pytest created its temp root inside the platform-native home "
        f"{native_home}: {tmp_path}"
    )


def test_default_profile_stays_outside_platform_native_home():
    from hermes_cli.profiles import get_profile_dir

    native_home = Path(hermes_constants._get_platform_default_hermes_home()).resolve()
    default_profile = get_profile_dir("default").resolve()

    assert not _is_within(default_profile, native_home), (
        f"the default profile resolved inside the platform-native home "
        f"{native_home}: {default_profile}"
    )


def test_sessionstart_handles_nonexistent_basetemp_without_strict_resolve(
    tmp_path, monkeypatch
):
    import tests.conftest as test_conftest

    native_home = tmp_path / "native"
    native_home.mkdir()
    requested = native_home / "pytest" / "missing" / "basetemp"
    safe_basetemp = tmp_path / "safe-basetemp"
    safe_basetemp.mkdir()
    factory = SimpleNamespace(_given_basetemp=requested)
    config = SimpleNamespace(
        _tmp_path_factory=factory,
        option=SimpleNamespace(basetemp=str(requested)),
    )
    resolve_strict_values = []
    original_resolve = Path.resolve

    def record_resolve(path, *args, **kwargs):
        if path == requested:
            resolve_strict_values.append(kwargs.get("strict"))
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(test_conftest, "_OPERATOR_PLATFORM_HOME", native_home)
    monkeypatch.setattr(
        test_conftest, "_make_test_home", lambda prefix: safe_basetemp
    )
    monkeypatch.setattr(Path, "resolve", record_resolve)

    assert not requested.exists()
    test_conftest.pytest_sessionstart(SimpleNamespace(config=config))

    assert resolve_strict_values == [False]
    assert factory._given_basetemp == safe_basetemp
    assert config.option.basetemp == str(safe_basetemp)


def test_sessionstart_reports_basetemp_resolve_failures(tmp_path, monkeypatch):
    import tests.conftest as test_conftest

    native_home = tmp_path / "native"
    native_home.mkdir()
    requested = native_home / "missing" / "basetemp"
    factory = SimpleNamespace(_given_basetemp=requested)
    config = SimpleNamespace(
        _tmp_path_factory=factory,
        option=SimpleNamespace(basetemp=str(requested)),
    )
    original_resolve = Path.resolve

    def fail_requested_resolve(path, *args, **kwargs):
        if path == requested:
            raise OSError("synthetic resolve failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(test_conftest, "_OPERATOR_PLATFORM_HOME", native_home)
    monkeypatch.setattr(Path, "resolve", fail_requested_resolve)

    with pytest.raises(RuntimeError, match="basetemp.*native") as exc_info:
        test_conftest.pytest_sessionstart(SimpleNamespace(config=config))
    assert str(requested) in str(exc_info.value)


def test_sessionstart_fails_closed_when_pytest_factory_seam_is_missing(
    tmp_path, monkeypatch
):
    import tests.conftest as test_conftest

    native_home = tmp_path / "native"
    native_home.mkdir()
    requested = native_home / "missing" / "basetemp"
    config = SimpleNamespace(
        _tmp_path_factory=SimpleNamespace(),
        option=SimpleNamespace(basetemp=str(requested)),
    )
    monkeypatch.setattr(test_conftest, "_OPERATOR_PLATFORM_HOME", native_home)

    with pytest.raises(RuntimeError, match="TempPathFactory|tmp_path factory"):
        test_conftest.pytest_sessionstart(SimpleNamespace(config=config))


def test_sessionstart_fails_closed_after_factory_materializes_unsafe_basetemp(
    tmp_path, monkeypatch
):
    import tests.conftest as test_conftest

    native_home = tmp_path / "native"
    native_home.mkdir()
    requested = native_home / "materialized" / "basetemp"
    factory = SimpleNamespace(_given_basetemp=requested, _basetemp=requested)
    config = SimpleNamespace(
        _tmp_path_factory=factory,
        option=SimpleNamespace(basetemp=str(requested)),
    )
    monkeypatch.setattr(test_conftest, "_OPERATOR_PLATFORM_HOME", native_home)

    with pytest.raises(RuntimeError, match="already materialized"):
        test_conftest.pytest_sessionstart(SimpleNamespace(config=config))


def test_native_home_capture_precedes_late_path_overrides(tmp_path, monkeypatch):
    import tests.conftest as test_conftest

    captured = test_conftest._OPERATOR_PLATFORM_HOME
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "late-home")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "late-localappdata"))
    late_native_home = Path(
        hermes_constants._get_platform_default_hermes_home()
    ).resolve(strict=False)

    assert test_conftest._OPERATOR_PLATFORM_HOME == captured
    assert captured != late_native_home


@pytest.mark.windows_only
def test_path_containment_is_case_insensitive_on_windows(tmp_path):
    native_home = (tmp_path / "NativeHermes").resolve()
    native_home.mkdir()
    differently_cased = Path(str(native_home).swapcase()) / "missing" / "child"

    assert _is_within(
        differently_cased.resolve(strict=False), native_home.resolve(strict=False)
    )


@pytest.mark.linux_only
def test_path_containment_is_case_sensitive_on_linux(tmp_path):
    native_home = (tmp_path / "NativeHermes").resolve()
    differently_cased = tmp_path / "nativehermes" / "missing"

    assert not _is_within(
        differently_cased.resolve(strict=False), native_home.resolve(strict=False)
    )


@pytest.mark.macos_only
def test_path_containment_is_case_sensitive_on_macos(tmp_path):
    native_home = (tmp_path / "NativeHermes").resolve()
    differently_cased = tmp_path / "nativehermes" / "missing"

    assert not _is_within(
        differently_cased.resolve(strict=False), native_home.resolve(strict=False)
    )

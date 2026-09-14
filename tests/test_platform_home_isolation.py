"""Regression tests for test-home isolation from the platform-native Hermes home."""

import os
from pathlib import Path

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

"""Portable holographic fact-DB paths across profiles.

Regression tests for the cloned-profile-shares-source-DB / renamed-profile-loses-facts
bug class: the stored ``db_path`` must be the portable ``$HERMES_HOME`` spelling that
resolves per active profile, and legacy concrete values pinning another profile's store
must heal to the active profile at runtime.
"""
from __future__ import annotations

from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic.paths import (
    PORTABLE_DB_PATH,
    is_stale_profile_pinned_path,
    normalize_db_path_for_save,
    resolve_db_path,
)


def _provider(config):
    return HolographicMemoryProvider(config=config)


def test_schema_default_is_portable(tmp_path, monkeypatch):
    """The setup wizard prompts the portable spelling, never a concrete home path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    schema = _provider({}).get_config_schema()
    default = next(f["default"] for f in schema if f["key"] == "db_path")
    assert default == PORTABLE_DB_PATH


def test_normalize_collapses_active_home_default(tmp_path):
    """Accepting the (old) concrete default still persists the portable form."""
    home_default = str(tmp_path / "memory_store.db")
    assert normalize_db_path_for_save(home_default, str(tmp_path)) == PORTABLE_DB_PATH
    assert normalize_db_path_for_save("", str(tmp_path)) == PORTABLE_DB_PATH
    assert normalize_db_path_for_save(PORTABLE_DB_PATH, str(tmp_path)) == PORTABLE_DB_PATH


def test_normalize_keeps_custom_paths(tmp_path):
    """Deliberate custom locations are never rewritten."""
    assert normalize_db_path_for_save("custom.db", str(tmp_path)) == "custom.db"
    assert normalize_db_path_for_save("/data/shared/custom.db", str(tmp_path)) == "/data/shared/custom.db"


def test_portable_resolves_per_profile(tmp_path):
    """A clone carrying the portable value opens its own DB, not the source's."""
    import os

    src = tmp_path / "profiles" / "src"
    clone = tmp_path / "profiles" / "clone"
    assert os.path.normcase(os.path.abspath(resolve_db_path(PORTABLE_DB_PATH, str(src)))) == os.path.normcase(
        os.path.abspath(str(src / "memory_store.db"))
    )
    assert os.path.normcase(os.path.abspath(resolve_db_path(PORTABLE_DB_PATH, str(clone)))) == os.path.normcase(
        os.path.abspath(str(clone / "memory_store.db"))
    )


def test_stale_clone_path_heals(tmp_path):
    """A legacy clone config pointing at the source DB resolves to the clone's own DB."""
    src = tmp_path / "profiles" / "src"
    clone = tmp_path / "profiles" / "clone"
    stale = str(src / "memory_store.db")
    assert is_stale_profile_pinned_path(stale, str(clone)) is True
    assert resolve_db_path(stale, str(clone)) == str(clone / "memory_store.db")


def test_stale_rename_path_heals(tmp_path):
    """A legacy renamed config pointing at the ghost old dir resolves to the new home."""
    old = tmp_path / "profiles" / "oldname"
    new = tmp_path / "profiles" / "newname"
    stale = str(old / "memory_store.db")
    assert is_stale_profile_pinned_path(stale, str(new)) is True
    assert resolve_db_path(stale, str(new)) == str(new / "memory_store.db")


def test_custom_filename_never_stale(tmp_path):
    """An intentional shared/custom DB keeps resolving where it was pointed."""
    home = tmp_path / "profiles" / "clone"
    assert is_stale_profile_pinned_path("/data/shared/custom.db", str(home)) is False
    assert resolve_db_path("/data/shared/custom.db", str(home)) == "/data/shared/custom.db"


def test_initialize_heals_stale_clone(tmp_path, monkeypatch):
    """The provider opens the clone's own store even with a legacy source-pinned config."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    src = tmp_path / "profiles" / "src"
    clone = tmp_path / "profiles" / "clone"
    clone.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(clone))
    token = set_hermes_home_override(str(clone))
    try:
        provider = _provider({"db_path": str(src / "memory_store.db")})
        provider.initialize("session-1")
        assert str(provider._store.db_path) == str(clone / "memory_store.db")
        provider.shutdown()
    finally:
        reset_hermes_home_override(token)


def test_save_config_writes_portable_default(tmp_path, monkeypatch):
    """Persisting the active home's concrete default stores the portable spelling."""
    import yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("memory:\n  provider: holographic\n")

    _provider({}).save_config({"db_path": str(tmp_path / "memory_store.db")}, str(tmp_path))

    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert raw["plugins"]["hermes-memory-store"]["db_path"] == PORTABLE_DB_PATH

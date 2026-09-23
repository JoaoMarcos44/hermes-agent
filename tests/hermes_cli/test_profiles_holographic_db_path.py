"""Clone/rename sanitization of the holographic fact-DB path.

A cloned profile must resolve its own fact DB (never the source's), and a renamed
profile must keep resolving its own (moved) facts instead of a ghost old directory.
Both flows rewrite a stale concrete ``plugins.hermes-memory-store.db_path`` to the
portable ``$HERMES_HOME`` spelling; custom paths are never touched.
"""
from __future__ import annotations

import yaml

from plugins.memory.holographic.paths import PORTABLE_DB_PATH


def _write_config(home, db_path):
    (home / "config.yaml").write_text(
        "plugins:\n  hermes-memory-store:\n    db_path: '" + db_path + "'\n",
        encoding="utf-8",
    )


def _read_db_path(home):
    return yaml.safe_load((home / "config.yaml").read_text())["plugins"]["hermes-memory-store"]["db_path"]


def test_clone_sanitize_resets_source_pinned_path(tmp_path):
    """A clone carrying the source's concrete DB path is reset to the portable form."""
    from hermes_cli.profiles import _sanitize_cloned_holographic_db_path

    source = tmp_path / "profiles" / "src"
    staging = tmp_path / "profiles" / "clone"
    staging.mkdir(parents=True)
    _write_config(staging, str(source / "memory_store.db"))

    _sanitize_cloned_holographic_db_path(staging, "clone", source)

    assert _read_db_path(staging) == PORTABLE_DB_PATH


def test_clone_sanitize_keeps_custom_path(tmp_path):
    """A deliberate custom DB location survives the clone untouched."""
    from hermes_cli.profiles import _sanitize_cloned_holographic_db_path

    source = tmp_path / "profiles" / "src"
    staging = tmp_path / "profiles" / "clone"
    staging.mkdir(parents=True)
    _write_config(staging, "/data/shared/custom.db")

    _sanitize_cloned_holographic_db_path(staging, "clone", source)

    assert _read_db_path(staging) == "/data/shared/custom.db"


def test_clone_sanitize_keeps_portable(tmp_path):
    """An already-portable clone config is left alone."""
    from hermes_cli.profiles import _sanitize_cloned_holographic_db_path

    source = tmp_path / "profiles" / "src"
    staging = tmp_path / "profiles" / "clone"
    staging.mkdir(parents=True)
    _write_config(staging, PORTABLE_DB_PATH)

    _sanitize_cloned_holographic_db_path(staging, "clone", source)

    assert _read_db_path(staging) == PORTABLE_DB_PATH


def test_rewrite_pins_source_dir_outside_standard_tree(tmp_path, monkeypatch):
    """Exact source knowledge covers custom HERMES_HOME layouts without heuristics."""
    from hermes_cli.profiles import _rewrite_holographic_db_path_to_portable

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom-root"))
    source = tmp_path / "custom-root"
    staging = tmp_path / "staging"
    staging.mkdir(parents=True)
    _write_config(staging, str(source / "memory_store.db"))

    assert _rewrite_holographic_db_path_to_portable(staging / "config.yaml", staging, source) is True
    assert _read_db_path(staging) == PORTABLE_DB_PATH

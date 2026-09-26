"""Restore-source admission for corrupt SQLite sources (issue #122868).

A corrupt snapshot/zip database must never replace a healthy live database,
and the operation must report the refused restore instead of an aggregate
success.  Both restore entry points are covered — ``restore_quick_snapshot``
(the ``/snapshot restore`` path) and ``run_import`` (``hermes import``) — and
every scenario runs in a multi-file home (state.db + config.yaml +
cron/jobs.json) so the partial-recovery reporting contract is pinned in the
same scenario: the refused database is left alone while the other files still
restore.

The ``confusable-uri`` damage puts a ``#`` in the damaged source's path and
plants a VALID decoy database at the pre-fragment path.  SQLite's URI parser
drops everything from ``#`` on, so an unescaped ``file:{path}?mode=ro``
validates and reads the DECOY instead of the file the caller named — that case
is red against the unpatched code and stays red if source validation is added
without making the read-only URI escape the path.
"""

import json
import os
import sqlite3
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PAGE_SIZE = 4096
_SNAP_ID = "20260925-120000"


@pytest.fixture(autouse=True)
def _no_real_gateway_service(monkeypatch):
    """run_import() auto-installs the gateway service post-restore; tests must
    never touch the host's systemd/launchd. Same stub as test_backup.py."""
    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "ensure_gateway_service", lambda **kw: False)
    monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)


def _install_home(tmp_path: Path, monkeypatch, name: str = "hermes") -> Path:
    """Point HERMES_HOME and Path.home() at an isolated home under tmp_path."""
    home = tmp_path / name
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _write_state_db(path: Path, marker: str, rows: int = 300) -> None:
    """Real multi-page SQLite database with the ``evidence`` table every
    assertion reads.  ``poison`` filler keeps the file multi-page and provides
    a corruption target whose damage breaks integrity_check while ``evidence``
    keeps serving reads (the held-but-corrupt destination state)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE evidence (value TEXT)")
        conn.execute("CREATE TABLE poison (value TEXT)")
        conn.execute("INSERT INTO evidence VALUES (?)", (marker,))
        conn.executemany(
            "INSERT INTO poison VALUES (?)", [(f"filler-{i}",) for i in range(rows)]
        )
        conn.commit()
    finally:
        conn.close()


def _page_size(raw: bytes) -> int:
    size = int.from_bytes(raw[16:18], "big")
    return 65536 if size == 1 else size


def _root_page(path: Path, table: str) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()[0]
    finally:
        conn.close()


def _damage_db(path: Path, mode: str) -> None:
    """Corrupt the database at *path* in place (only ever called on copies)."""
    if mode == "header":
        raw = bytearray(path.read_bytes())
        raw[:16] = b"\xde\xad\xbe\xef" * 4
        path.write_bytes(bytes(raw))
        return
    if mode == "truncated":
        path.write_bytes(path.read_bytes()[:_PAGE_SIZE])
        return
    # "btree" and "confusable-uri": flip the evidence root page's b-tree
    # page-type byte.  The header stays intact, so detecting this damage
    # depends on the read-only PRAGMA — exactly the check an unescaped URI
    # redirects at a decoy.
    raw = bytearray(path.read_bytes())
    root = _root_page(path, "evidence")
    raw[(_page_size(raw) * (root - 1))] = 0xFF
    path.write_bytes(bytes(raw))


def _poison_state_db(path: Path) -> None:
    """Damage only the ``poison`` table's root page: integrity_check fails
    while ``evidence`` reads keep working."""
    raw = bytearray(path.read_bytes())
    root = _root_page(path, "poison")
    raw[(_page_size(raw) * (root - 1))] = 0xFF
    path.write_bytes(bytes(raw))


def _build_home(home: Path, *, db: str = "live") -> Path:
    """Multi-file home: state.db (evidence='live') + config.yaml + cron/jobs.json.

    ``db``: 'live' (healthy), 'poison' (fails integrity_check, still readable),
    'missing' (no state.db at all)."""
    home.mkdir(parents=True, exist_ok=True)
    if db != "missing":
        _write_state_db(home / "state.db", "live")
        if db == "poison":
            _poison_state_db(home / "state.db")
    (home / "config.yaml").write_text("mode: live\n")
    (home / "cron").mkdir(exist_ok=True)
    (home / "cron" / "jobs.json").write_text('{"jobs": [{"id": "live-job"}]}\n')
    return home


def _build_snapshot(home: Path, snap_id: str, db_mode: str) -> Path:
    """Hand-built quick snapshot: manifest + config.yaml + cron/jobs.json +
    a state.db copy damaged per *db_mode* ('ok' = valid)."""
    snap = home / "state-snapshots" / snap_id
    (snap / "cron").mkdir(parents=True)
    (snap / "manifest.json").write_text(json.dumps({
        "id": snap_id,
        "files": {"state.db": {}, "config.yaml": {}, "cron/jobs.json": {}},
    }))
    (snap / "config.yaml").write_text("mode: snapshot\n")
    (snap / "cron" / "jobs.json").write_text('{"jobs": [{"id": "snapshot-job"}]}\n')
    snap_db = snap / "state.db"
    _write_state_db(snap_db, "snapshot")
    if db_mode != "ok":
        _damage_db(snap_db, db_mode)
    return snap


def _build_zip(zip_path: Path, db_mode: str) -> None:
    """Backup zip with config.yaml + cron/jobs.json + a state.db member
    damaged per *db_mode* ('ok' = valid)."""
    staged = zip_path.parent / "staged.db"
    _write_state_db(staged, "snapshot")
    if db_mode != "ok":
        _damage_db(staged, db_mode)
    try:
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(staged, arcname="state.db")
            zf.writestr("config.yaml", "mode: archive\n")
            zf.writestr("cron/jobs.json", '{"jobs": [{"id": "archive-job"}]}\n')
    finally:
        staged.unlink(missing_ok=True)


def _pre_fragment(path_str: str) -> Path:
    """The path SQLite's URI parser actually opens for ``file:{path}``: it
    drops everything from the first ``#``."""
    return Path(path_str.split("#", 1)[0])


def _evidence_rows(path: Path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT value FROM evidence").fetchall()
    finally:
        conn.close()


def _integrity_rows(path: Path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        # A database too malformed for the PRAGMA to answer is not "ok" by any
        # reading; surface the refusal as a comparable row.
        return [f"error: {exc}"]
    finally:
        conn.close()


def _run_snapshot_restore(home: Path, snap_id: str):
    from hermes_cli.backup import restore_quick_snapshot

    return restore_quick_snapshot(snap_id, hermes_home=home)


def _run_snapshot_cli(snap_id: str) -> None:
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    class _Stub(CLICommandsMixin):
        def __init__(self):
            self.agent = None
            self._session_db = None

    _Stub()._snapshot_restore(["snapshot", "restore", snap_id])


def _run_import(zip_path: Path):
    from hermes_cli.backup import run_import

    return run_import(Namespace(zipfile=str(zip_path), force=True))


# ---------------------------------------------------------------------------
# Corrupt source must be refused and reported (issue #122868)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["snapshot", "import"])
@pytest.mark.parametrize("damage", ["header", "btree", "truncated", "confusable-uri"])
def test_corrupt_source_cannot_replace_a_healthy_database(
    tmp_path, monkeypatch, entry, damage
):
    """A corrupt snapshot/zip state.db must not replace the live one — not a
    byte, not the inode (#65942) — and the operation must report failure even
    though the home's other files restore fine.

    The confusable-uri case puts the ``#`` where each entry point's source path
    picks it up: in the snapshot directory name for /snapshot restore, in the
    home directory name for hermes import (whose restore source is a staged
    temp file under the home).  In both cases a VALID decoy database sits at
    the pre-fragment path the unescaped URI would open instead.
    """
    if damage == "confusable-uri":
        # `#`-bearing source paths: snapshot dir name for the snapshot entry,
        # home dir name for the import entry (the staged temp inherits it).
        home = _install_home(tmp_path, monkeypatch, "hermes" if entry == "snapshot" else "hermes#home")
        snap_id = f"{_SNAP_ID}#damaged" if entry == "snapshot" else _SNAP_ID
    else:
        home = _install_home(tmp_path, monkeypatch)
        snap_id = _SNAP_ID

    _build_home(home)

    if entry == "snapshot":
        snap = _build_snapshot(home, snap_id, db_mode=damage)
        source_path = str(snap / "state.db")
    else:
        snap = None
        zip_path = tmp_path / "backup.zip"
        _build_zip(zip_path, db_mode=damage)
        source_path = str(home / "state.db")

    if damage == "confusable-uri":
        # The premise of this case: a VALID decoy sits exactly where SQLite's
        # URI parser would cut the path, so an unescaped read-only URI
        # validates/reads the decoy instead of the damaged file.
        decoy = _pre_fragment(source_path)
        assert decoy != Path(source_path)
        _write_state_db(decoy, "decoy")

    live_db = home / "state.db"
    before_bytes = live_db.read_bytes()
    before_ino = os.stat(live_db).st_ino

    if entry == "snapshot":
        result = _run_snapshot_restore(home, snap_id)
        assert result is False, (
            "restore_quick_snapshot reported success while refusing/restoring "
            "over a corrupt state.db source"
        )
    else:
        result = _run_import(zip_path)
        assert result == 1, (
            "run_import reported success while refusing/restoring over a "
            "corrupt state.db member"
        )

    # The live database must be byte-identical and on the same inode (#65942),
    # still serving its own rows.
    assert live_db.read_bytes() == before_bytes, (
        "live state.db bytes were replaced from a corrupt restore source"
    )
    assert os.stat(live_db).st_ino == before_ino, (
        "live state.db inode was replaced by a restore from a corrupt source"
    )
    assert _evidence_rows(live_db) == [("live",)]

    # Partial recovery survives: the multi-file home's other files still
    # restore from the same snapshot/archive; only the database is left alone.
    if entry == "snapshot":
        assert (home / "config.yaml").read_text() == "mode: snapshot\n"
        assert json.loads((home / "cron" / "jobs.json").read_text()) == {
            "jobs": [{"id": "snapshot-job"}]
        }
    else:
        assert (home / "config.yaml").read_text() == "mode: archive\n"
        assert json.loads((home / "cron" / "jobs.json").read_text()) == {
            "jobs": [{"id": "archive-job"}]
        }


# ---------------------------------------------------------------------------
# Manifest settlement: every declared member contributes an outcome
# ---------------------------------------------------------------------------

def _snapshot_with_manifest(home: Path, files: dict[str, dict]) -> Path:
    snap = home / "state-snapshots" / _SNAP_ID
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text(json.dumps({"id": _SNAP_ID, "files": files}))
    return snap


def _assert_cli_incomplete(capsys) -> None:
    out = capsys.readouterr().out
    assert "Restored state from" not in out
    assert "Snapshot not found" not in out
    assert "restore incomplete" in out.lower()


def test_missing_declared_db_keeps_valid_sibling_but_reports_incomplete(
    tmp_path, monkeypatch, capsys
):
    home = _install_home(tmp_path, monkeypatch)
    _build_home(home)
    before_db = (home / "state.db").read_bytes()
    snap = _snapshot_with_manifest(home, {"state.db": {}, "config.yaml": {}})
    (snap / "config.yaml").write_text("mode: snapshot\n")

    _run_snapshot_cli(_SNAP_ID)

    assert (home / "state.db").read_bytes() == before_db
    assert (home / "config.yaml").read_text() == "mode: snapshot\n"
    _assert_cli_incomplete(capsys)


def test_source_traversal_keeps_valid_sibling_but_reports_incomplete(
    tmp_path, monkeypatch, capsys
):
    home = _install_home(tmp_path, monkeypatch)
    _build_home(home)
    snap = _snapshot_with_manifest(home, {"../outside.txt": {}, "config.yaml": {}})
    (snap / "config.yaml").write_text("mode: snapshot\n")
    escaped_source = snap.parent / "outside.txt"
    escaped_source.write_text("must not publish\n")

    _run_snapshot_cli(_SNAP_ID)

    assert not (home.parent / "outside.txt").exists()
    assert escaped_source.read_text() == "must not publish\n"
    assert (home / "config.yaml").read_text() == "mode: snapshot\n"
    _assert_cli_incomplete(capsys)


def test_destination_escape_keeps_valid_sibling_but_reports_incomplete(
    tmp_path, monkeypatch, capsys
):
    home = _install_home(tmp_path, monkeypatch)
    _build_home(home)
    snap = _snapshot_with_manifest(home, {"escape/blocked.txt": {}, "config.yaml": {}})
    (snap / "escape").mkdir()
    (snap / "escape" / "blocked.txt").write_text("snapshot bytes\n")
    (snap / "config.yaml").write_text("mode: snapshot\n")

    outside = tmp_path / "outside-destination"
    outside.mkdir()
    try:
        (home / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    _run_snapshot_cli(_SNAP_ID)

    assert not (outside / "blocked.txt").exists()
    assert (home / "config.yaml").read_text() == "mode: snapshot\n"
    _assert_cli_incomplete(capsys)


def test_valid_multifile_snapshot_reports_success_through_cli(
    tmp_path, monkeypatch, capsys
):
    home = _install_home(tmp_path, monkeypatch)
    _build_home(home)
    snap = _build_snapshot(home, _SNAP_ID, db_mode="ok")

    _run_snapshot_cli(_SNAP_ID)

    out = capsys.readouterr().out
    assert f"Restored state from: {_SNAP_ID}" in out
    assert "incomplete" not in out.lower()
    assert _evidence_rows(home / "state.db") == [("snapshot",)]
    assert (home / "config.yaml").read_text() == "mode: snapshot\n"
    assert json.loads((home / "cron" / "jobs.json").read_text()) == {
        "jobs": [{"id": "snapshot-job"}]
    }


def test_malformed_manifest_is_existing_but_incomplete(
    tmp_path, monkeypatch, capsys
):
    home = _install_home(tmp_path, monkeypatch)
    _build_home(home)
    before_db = (home / "state.db").read_bytes()
    snap = home / "state-snapshots" / _SNAP_ID
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text("{not-json")

    _run_snapshot_cli(_SNAP_ID)

    assert (home / "state.db").read_bytes() == before_db
    _assert_cli_incomplete(capsys)


# ---------------------------------------------------------------------------
# Feature-utility control: valid sources must still restore everywhere
# (this test must pass against the unpatched code too)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "destination", ["healthy", "corrupt", "held", "missing-target-import"]
)
def test_valid_source_still_restores_every_destination_state(
    tmp_path, monkeypatch, destination
):
    """A VALID state.db source must keep restoring over every destination
    state: a healthy live database, a corrupt-but-readable one, one held open
    by a live connection (the #65942 'visible through an existing live
    connection' expectation), and — for hermes import into a fresh home — no
    destination at all, where the member must still be published.

    This is the feature-utility control for the source-admission guard: it
    passes against the unpatched code and must keep passing once restore
    sources are validated.
    """
    if destination == "missing-target-import":
        home = _install_home(tmp_path, monkeypatch)
        _build_home(home, db="missing")
        zip_path = tmp_path / "backup.zip"
        _build_zip(zip_path, db_mode="ok")

        assert not _run_import(zip_path), "import of a valid .db member failed"

        live_db = home / "state.db"
        assert live_db.exists(), "valid .db member was not published into the fresh home"
        assert _integrity_rows(live_db) == [("ok",)]
        assert _evidence_rows(live_db) == [("snapshot",)]
        return

    db_state = {"healthy": "live", "corrupt": "poison", "held": "live"}[destination]
    for entry in ("snapshot", "import"):
        home = _install_home(tmp_path, monkeypatch, f"hermes-{entry}")
        _build_home(home, db=db_state)
        live_db = home / "state.db"
        if destination == "corrupt":
            assert _integrity_rows(live_db) != [("ok",)], "fixture must start corrupt"

        # A live connection held across the restore — the shape the issue's
        # 'visible through an existing live connection' expectation names.
        held = sqlite3.connect(str(live_db))
        try:
            if destination == "held":
                held.execute("PRAGMA journal_mode=wal")
            assert held.execute("SELECT value FROM evidence").fetchall() == [("live",)]
            if destination == "held":
                # Post-snapshot write through the held connection: the restore
                # must revert it and the held connection must see the revert.
                held.execute("INSERT INTO evidence VALUES ('live-extra')")
                held.commit()

            if entry == "snapshot":
                snap = _build_snapshot(home, _SNAP_ID, db_mode="ok")
                result = _run_snapshot_restore(home, snap.name)
                assert result is True, "restore_quick_snapshot refused a valid source"
            else:
                zip_path = tmp_path / f"backup-{entry}.zip"
                _build_zip(zip_path, db_mode="ok")
                assert not _run_import(zip_path), "run_import failed on a valid source"

            assert held.execute("SELECT value FROM evidence").fetchall() == [("snapshot",)], (
                f"live connection did not converge on the restored rows "
                f"(destination={destination}, entry={entry})"
            )
            assert _integrity_rows(live_db) == [("ok",)]
        finally:
            held.close()

"""Behavioral tests for quiescent recovery installation of the active state.db."""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from pathlib import Path

from hermes_state import SessionDB


def _seed_state_db(path: Path, count: int = 12) -> None:
    db = SessionDB(db_path=path)
    try:
        for index in range(count):
            session_id = f"recovery-{index}"
            db.create_session(session_id=session_id, source="cli")
            db.append_message(session_id, role="user", content=f"preservedneedle{index}")
    finally:
        db.close()


def _corrupt_fts_data_root(path: Path) -> None:
    """Damage a real derived-index B-tree while leaving canonical rows readable."""
    conn = sqlite3.connect(str(path))
    try:
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root_page = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'messages_fts_data'"
        ).fetchone()[0]
    finally:
        conn.close()
    with path.open("r+b") as handle:
        handle.seek(page_size * (root_page - 1))
        handle.write(b"\xde\xad\xbe\xef" * (page_size // 4))


def test_install_recovers_real_corruption_under_quiescent_writer_guard(tmp_path, monkeypatch, capsys):
    """A complete candidate is installed only through the real exclusive DB guard."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = home / "state.db"
    output = tmp_path / "recovered-state.db"
    report_path = output.with_name(output.name + ".recovery.json")
    _seed_state_db(source)
    _corrupt_fts_data_root(source)

    from hermes_cli.sessions_cmd import _cmd_recover

    status = _cmd_recover(Namespace(
        source=source,
        output=output,
        inspect_only=False,
        allow_partial=False,
        install=True,
        report=report_path,
        work_dir=tmp_path,
        chunk_size=1000,
    ))

    assert status == 0, capsys.readouterr().out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["partial"] is False
    assert report["verified"] is True
    assert report["installed"] is True
    assert report["install_target"] == str(source)
    assert report["generation_fence"]["source_application_id"] != report["generation_fence"]["candidate_application_id"]

    preserved = Path(report["preserved_source_bundle"])
    assert preserved.is_dir()
    assert (preserved / "source" / "state.db").is_file()
    assert (preserved / "manifest.json").is_file()

    db = SessionDB(db_path=source)
    try:
        assert db.search_messages("preservedneedle3")
        db.create_session(session_id="recovery-canary", source="system")
        db.append_message("recovery-canary", role="user", content="recoverycanaryneedle")
        assert db.get_messages("recovery-canary")[0]["content"] == "recoverycanaryneedle"
        assert db.search_messages("recoverycanaryneedle")
        db.delete_session("recovery-canary")
    finally:
        db.close()

    assert "installed" in capsys.readouterr().out.lower()


def test_install_refuses_when_a_real_sessiondb_writer_is_open(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = home / "state.db"
    output = tmp_path / "must-not-install.db"
    _seed_state_db(source)
    # Damage a canonical B-tree, then hold a real writable SessionDB connection.
    conn = sqlite3.connect(str(source))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root_page = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'sessions'"
        ).fetchone()[0]
    finally:
        conn.close()
    with source.open("r+b") as handle:
        handle.seek(page_size * (root_page - 1))
        handle.write(b"\\xde\\xad\\xbe\\xef" * (page_size // 4))
    writer = SessionDB(db_path=source)

    from hermes_cli.sessions_cmd import _cmd_recover

    try:
        status = _cmd_recover(Namespace(
            source=source,
            output=output,
            inspect_only=False,
            allow_partial=False,
            install=True,
            report=None,
            work_dir=tmp_path,
            chunk_size=1000,
        ))
        shown = capsys.readouterr().out.lower()
        assert status != 0
        assert "connection" in shown or "writer" in shown
        assert not output.exists()
    finally:
        writer.close()

    with sqlite3.connect(str(source)) as check:
        result = check.execute("PRAGMA quick_check").fetchone()[0]
    assert result != "ok"


def test_install_does_not_allow_partial_recovery(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    source = home / "state.db"
    output = tmp_path / "must-not-install-partial.db"
    _seed_state_db(source)

    from hermes_cli.sessions_cmd import _cmd_recover

    status = _cmd_recover(Namespace(
        source=source,
        output=output,
        inspect_only=False,
        allow_partial=True,
        install=True,
        report=None,
        work_dir=tmp_path,
        chunk_size=1000,
    ))

    assert status == 2
    assert "cannot be combined" in capsys.readouterr().out.lower()
    assert not output.exists()


def test_exclusive_repair_guard_refuses_a_real_sessiondb_writer(tmp_path):
    """The existing cross-platform SQLite guard refuses while a writer connection is open."""
    import hermes_state_repair

    source = tmp_path / "state.db"
    _seed_state_db(source, count=1)
    writer = SessionDB(db_path=source)
    try:
        with hermes_state_repair._exclusive_repair_db_guard(source) as (guard, error):
            assert guard is None
            assert error is not None
    finally:
        writer.close()


def test_sessions_recover_parser_exposes_install_as_an_explicit_opt_in():
    from argparse import ArgumentParser

    from hermes_cli.subcommands.sessions import build_sessions_parser

    parser = ArgumentParser()
    subparsers = parser.add_subparsers()
    build_sessions_parser(subparsers, cmd_sessions=lambda args: args)
    args = parser.parse_args([
        "sessions", "recover", "--source", "state.db", "--output", "recovered.db", "--install",
    ])

    assert args.install is True

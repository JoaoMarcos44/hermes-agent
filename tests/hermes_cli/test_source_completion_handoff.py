from pathlib import Path

from hermes_cli import source_completion_handoff


def test_success_clears_pending_marker(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    pending = tmp_path / "pending"
    pending.write_text("owed\n")
    cleared = []

    import pm.environments
    from hermes_cli import source_completion, venv_sync

    monkeypatch.setattr(pm.environments, "activate_dependencies", lambda _root: None)
    monkeypatch.setattr(venv_sync, "completion_pending_path", lambda _root: pending)
    monkeypatch.setattr(venv_sync, "clear_completion", lambda _root: cleared.append(Path(_root)))
    monkeypatch.setattr(source_completion, "complete_source_checkout", lambda *a, **kw: True)

    assert source_completion_handoff.main(["--source", str(root)]) == 0
    assert cleared == [root]


def test_failure_preserves_pending_marker(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    pending = tmp_path / "pending"
    pending.write_text("owed\n")

    import pm.environments
    from hermes_cli import source_completion, venv_sync

    monkeypatch.setattr(pm.environments, "activate_dependencies", lambda _root: None)
    monkeypatch.setattr(venv_sync, "completion_pending_path", lambda _root: pending)
    monkeypatch.setattr(venv_sync, "clear_completion", lambda _root: (_ for _ in ()).throw(AssertionError("cleared failed tail")))
    monkeypatch.setattr(source_completion, "complete_source_checkout", lambda *a, **kw: False)

    assert source_completion_handoff.main(["--source", str(root)]) == 1
    assert pending.is_file()


def test_already_completed_obligation_is_a_noop(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    pending = tmp_path / "missing"

    import pm.environments
    from hermes_cli import source_completion, venv_sync

    monkeypatch.setattr(pm.environments, "activate_dependencies", lambda _root: None)
    monkeypatch.setattr(venv_sync, "completion_pending_path", lambda _root: pending)
    monkeypatch.setattr(source_completion, "complete_source_checkout", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("rebuilt cleared obligation")))

    assert source_completion_handoff.main(["--source", str(root)]) == 0

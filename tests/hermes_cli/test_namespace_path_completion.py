"""Regression for #42016: explicit context completions survive Windows mount errors."""

import os

import pytest
from prompt_toolkit.document import Document

from hermes_cli.commands_completion import SlashCommandCompleter


def _completion_texts(word):
    document = Document(word, cursor_position=len(word))
    return [item.text for item in SlashCommandCompleter().get_completions(document, None)]


@pytest.mark.platforms("windows")
def test_device_namespace_explicit_file_and_folder_completions(tmp_path, monkeypatch):
    import hermes_cli.commands_completion as commands_mod

    slash = os.sep
    (tmp_path / "device-note.txt").write_text("device", encoding="utf-8")
    (tmp_path / "device-folder").mkdir()
    monkeypatch.chdir(tmp_path)

    drive, tail = os.path.splitdrive(str(tmp_path))
    device_dir = slash * 2 + "." + slash + drive + tail
    volume_dir = (
        slash * 2 + "?" + slash
        + "Volume{11111111-2222-3333-4444-555555555555}" + slash + "completion-fixture"
    )
    original_listdir = commands_mod.os.listdir
    original_isdir = commands_mod.os.path.isdir
    original_getsize = commands_mod.os.path.getsize

    def _listdir(path):
        if path.casefold() == volume_dir.casefold():
            return ["device-note.txt", "device-folder"]
        return original_listdir(path)

    def _isdir(path):
        if path.casefold().startswith((volume_dir + slash).casefold()):
            return path.rstrip(slash + "/").casefold().endswith(slash + "device-folder")
        return original_isdir(path)

    def _getsize(path):
        if path.casefold().startswith((volume_dir + slash).casefold()):
            return len("device")
        return original_getsize(path)

    monkeypatch.setattr(commands_mod.os, "listdir", _listdir)
    monkeypatch.setattr(commands_mod.os.path, "isdir", _isdir)
    monkeypatch.setattr(commands_mod.os.path, "getsize", _getsize)

    failures = []
    for directory in (device_dir, volume_dir):
        for context_prefix, child, suffix in (
            ("@file:", "device-note.txt", ""),
            ("@folder:", "device-folder", "/"),
        ):
            word = context_prefix + directory + os.sep
            expected = word + child + suffix
            try:
                texts = _completion_texts(word)
            except ValueError as exc:
                failures.append(f"{word!r} raised {exc!r}")
                continue
            if expected not in texts:
                failures.append(f"{word!r} omitted {expected!r}: {texts!r}")

    assert not failures, "\n".join(failures)


@pytest.mark.platforms("windows")
def test_unc_explicit_file_and_folder_completions(tmp_path, monkeypatch):
    import hermes_cli.commands_completion as commands_mod

    slash = os.sep
    monkeypatch.chdir(tmp_path)
    unc_dir = slash * 2 + "server" + slash + "share" + slash + "completion-fixture"
    entries = ("remote-note.txt", "remote-folder")

    def _listdir(path):
        assert path.casefold() == unc_dir.casefold()
        return entries

    def _isdir(path):
        return path.casefold().endswith(slash + "remote-folder")

    monkeypatch.setattr(commands_mod.os, "listdir", _listdir)
    monkeypatch.setattr(commands_mod.os.path, "isdir", _isdir)
    monkeypatch.setattr(commands_mod.os.path, "getsize", lambda _path: 1)

    failures = []
    for context_prefix, child, suffix in (
        ("@file:", "remote-note.txt", ""),
        ("@folder:", "remote-folder", "/"),
    ):
        word = context_prefix + unc_dir + os.sep
        expected = word + child + suffix
        try:
            texts = _completion_texts(word)
        except ValueError as exc:
            failures.append(f"{word!r} raised {exc!r}")
            continue
        if expected not in texts:
            failures.append(f"{word!r} omitted {expected!r}: {texts!r}")

    assert not failures, "\n".join(failures)

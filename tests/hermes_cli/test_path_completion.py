"""Tests for file path autocomplete in the CLI completer."""

import os
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_plain_text

from hermes_cli.commands_completion import SlashCommandCompleter, _file_size_label


def _display_names(completions):
    """Extract plain-text display names from a list of Completion objects."""
    return [to_plain_text(c.display) for c in completions]


def _display_metas(completions):
    """Extract plain-text display_meta from a list of Completion objects."""
    return [to_plain_text(c.display_meta) if c.display_meta else "" for c in completions]


@pytest.fixture
def completer():
    return SlashCommandCompleter()


class TestExtractPathWord:
    def test_relative_path(self):
        assert SlashCommandCompleter._extract_path_word("look at ./src/main.py") == "./src/main.py"





class TestPathCompletions:
    def test_lists_current_directory(self, tmp_path):
        (tmp_path / "file_a.py").touch()
        (tmp_path / "file_b.txt").touch()
        (tmp_path / "subdir").mkdir()

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            completions = list(SlashCommandCompleter._path_completions("./"))
            names = _display_names(completions)
            assert "file_a.py" in names
            assert "file_b.txt" in names
            assert "subdir/" in names
        finally:
            os.chdir(old_cwd)


    def test_directories_have_trailing_slash(self, tmp_path):
        (tmp_path / "mydir").mkdir()
        (tmp_path / "myfile.txt").touch()

        completions = list(SlashCommandCompleter._path_completions(f"{tmp_path}/"))
        names = _display_names(completions)
        metas = _display_metas(completions)
        assert "mydir/" in names
        idx = names.index("mydir/")
        assert metas[idx] == "dir"






class TestIntegration:
    """Test the completer produces path completions via the prompt_toolkit API."""

    def test_slash_commands_still_work(self, completer):
        doc = Document("/hel", cursor_position=4)
        event = MagicMock()
        completions = list(completer.get_completions(doc, event))
        names = _display_names(completions)
        assert "/help" in names

    def test_path_completion_triggers_on_dot_slash(self, completer, tmp_path):
        (tmp_path / "test.py").touch()
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            doc = Document("edit ./te", cursor_position=9)
            event = MagicMock()
            completions = list(completer.get_completions(doc, event))
            names = _display_names(completions)
            assert "test.py" in names
        finally:
            os.chdir(old_cwd)


    def test_url_does_not_touch_filesystem(self, completer, monkeypatch):
        # Regression for laggy typing: a URL token contains "/", so before the
        # scheme guard it reached _path_completions and called os.listdir on
        # every keystroke. Assert no completions AND that the filesystem is
        # never touched while a URL is under the cursor.
        import hermes_cli.commands_completion as commands_mod

        def _fail(*_args, **_kwargs):
            raise AssertionError("os.listdir must not run for a URL token")

        monkeypatch.setattr(commands_mod.os, "listdir", _fail)

        text = "open https://paste.rs/abc"
        doc = Document(text, cursor_position=len(text))
        event = MagicMock()
        assert list(completer.get_completions(doc, event)) == []


class TestExplicitAtPathCompletion:
    @pytest.mark.skipif(os.name != "nt", reason="requires a real Windows extended-length path")
    def test_extended_drive_file_completion_is_workspace_relative_and_attachable(
        self, tmp_path, monkeypatch, _isolate_hermes_home,
    ):
        from agent.context_references import preprocess_context_references

        monkeypatch.chdir(tmp_path)
        target = tmp_path / "extended-file.txt"
        target.write_text("extended file content", encoding="utf-8")
        extended = chr(92) * 2 + "?" + chr(92) + str(target)
        assert os.path.isfile(extended), extended

        completions = list(SlashCommandCompleter()._context_completions("@file:" + extended))
        suggestion = next(c.text for c in completions if c.text.endswith(target.name))
        assert suggestion == "@file:" + target.name

        result = preprocess_context_references(suggestion, cwd=tmp_path, context_length=8192)
        assert result.expanded and "extended file content" in result.message, result

    @pytest.mark.skipif(os.name != "nt", reason="requires a real Windows extended-length path")
    def test_extended_drive_folder_completion_is_workspace_relative_and_attachable(
        self, tmp_path, monkeypatch, _isolate_hermes_home,
    ):
        from agent.context_references import preprocess_context_references

        monkeypatch.chdir(tmp_path)
        root = tmp_path / "extended-folder"
        child = root / "child-folder"
        child.mkdir(parents=True)
        (child / "inside.txt").write_text("inside", encoding="utf-8")
        extended = chr(92) * 2 + "?" + chr(92) + str(root) + os.sep
        assert os.path.isdir(extended), extended

        completions = list(SlashCommandCompleter()._context_completions("@folder:" + extended))
        suggestion = next(c.text for c in completions if c.text.endswith("child-folder/"))
        expected = "@folder:" + os.path.join("extended-folder", "child-folder") + "/"
        assert suggestion == expected

        result = preprocess_context_references(suggestion, cwd=tmp_path, context_length=8192)
        assert result.expanded and "inside.txt" in result.message, result


class TestFileSizeLabel:
    def test_nonexistent(self):
        assert _file_size_label("/nonexistent_xyz") == ""

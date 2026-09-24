"""Regression coverage for never-stamped config.yaml migration semantics (#120813)."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parents[2] / "cli-config.yaml.example"


def _at(raw: dict, dotted: str):
    for part in dotted.split("."):
        raw = raw.get(part) if isinstance(raw, dict) else None
    return raw


def test_unversioned_config_preserves_current_user_choices_and_migrates_legacy_keys(
    tmp_path, monkeypatch
):
    from hermes_cli.config import DEFAULT_CONFIG, migrate_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    raw = {
        "display": {
            "personality": "kawaii",
            "background_process_notifications": "all",
        },
        "delegation": {
            "max_concurrent_children": 3,
            "max_iterations": 50,
        },
        "agent": {"verify_on_stop": True},
        "curator": {
            "stale_after_days": 30,
            "archive_after_days": 90,
        },
        "model_catalog": {"ttl_hours": 24},
        # A real legacy-key migration must still run on never-stamped files.
        "compression": {"summary_model": "custom/legacy-summary-model"},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    plugin = tmp_path / "plugins" / "notes-helper"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "name: notes-helper\nversion: 0.1.0\n", encoding="utf-8"
    )

    watched = (
        "display.personality",
        "display.background_process_notifications",
        "delegation.max_concurrent_children",
        "delegation.max_iterations",
        "agent.verify_on_stop",
        "curator.stale_after_days",
        "curator.archive_after_days",
        "model_catalog.ttl_hours",
    )
    before = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    migrate_config(interactive=False, quiet=True)

    after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert {key: _at(after, key) for key in watched} == {
        key: _at(before, key) for key in watched
    }
    assert _at(after, "plugins.enabled") is None
    assert "summary_model" not in after.get("compression", {})
    assert _at(after, "auxiliary.compression.model") == "custom/legacy-summary-model"
    assert after["_config_version"] == DEFAULT_CONFIG["_config_version"]


def test_unversioned_config_does_not_authorize_external_soul_migration(tmp_path, monkeypatch):
    """A missing config stamp says nothing about the age or ownership of SOUL.md."""
    from hermes_cli.config import migrate_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text("display:\n  personality: kawaii\n", encoding="utf-8")
    soul = tmp_path / "SOUL.md"
    original = (
        "# Me\n\n"
        "## Messaging other agents\n"
        "This is user-authored text in a current SOUL, not migration evidence.\n\n"
        "## Preferences\nKeep this too.\n"
    )
    soul.write_text(original, encoding="utf-8")

    migrate_config(interactive=False, quiet=True)

    assert soul.read_text(encoding="utf-8") == original


def test_unversioned_runner_uses_step_metadata_instead_of_a_version_allowlist(monkeypatch):
    import hermes_cli.config_migrations as migrations

    calls = []

    def safe(_results, _quiet):
        calls.append("safe")

    def unsafe(_results, _quiet):
        calls.append("unsafe")

    safe = migrations._evidence_gated(safe)
    monkeypatch.setattr(migrations, "MIGRATIONS", ((1, safe), (2, unsafe)))

    results = {"env_added": [], "config_added": [], "warnings": []}
    migrations.run_migrations(0, results, quiet=True, unversioned=True)
    assert calls == ["safe"]

    calls.clear()
    migrations.run_migrations(0, results, quiet=True)
    assert calls == ["safe", "unsafe"]


def test_template_seed_is_versioned_at_the_current_schema(tmp_path, monkeypatch):
    from hermes_cli.config import check_config_version

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    shutil.copy(TEMPLATE, tmp_path / "config.yaml")

    current, latest = check_config_version(raise_on_parse_error=True)
    assert current == latest

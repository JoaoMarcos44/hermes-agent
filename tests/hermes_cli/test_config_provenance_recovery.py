"""Regression coverage for config provenance ownership and recovery (#120813)."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml


TEMPLATE = Path(__file__).resolve().parents[2] / "cli-config.yaml.example"


def _at(raw: dict, dotted: str):
    for part in dotted.split("."):
        raw = raw.get(part) if isinstance(raw, dict) else None
    return raw


def test_unversioned_recovery_preserves_choices_and_repairs_only_config_evidence(
    tmp_path, monkeypatch
):
    from hermes_cli.config import DEFAULT_CONFIG, migrate_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
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
                # Positive config-local evidence: this retired key is safe to repair.
                "compression": {"summary_model": "custom/legacy-summary"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    plugin = tmp_path / "plugins" / "notes-helper"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "name: notes-helper\nversion: 0.1.0\n", encoding="utf-8"
    )

    # External artifacts have independent provenance and are not authorized by config.yaml.
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_MODEL=user-tool-value\n", encoding="utf-8")
    soul_path = tmp_path / "SOUL.md"
    soul_text = (
        "# Me\n\n"
        "## Messaging other agents\n"
        "User-authored current content; the heading alone is not migration provenance.\n\n"
        "## Preferences\nKeep this too.\n"
    )
    soul_path.write_text(soul_text, encoding="utf-8")

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
    assert _at(after, "auxiliary.compression.model") == "custom/legacy-summary"
    assert after["_config_version"] == DEFAULT_CONFIG["_config_version"]
    assert env_path.read_text(encoding="utf-8") == "OPENAI_MODEL=user-tool-value\n"
    assert soul_path.read_text(encoding="utf-8") == soul_text


def test_new_file_writer_owns_schema_provenance(tmp_path):
    """RED on #120814: its canonical writer still creates an unstamped config."""
    from hermes_cli.config import DEFAULT_CONFIG, atomic_config_write

    config_path = tmp_path / "config.yaml"
    atomic_config_write(config_path, {"display": {"personality": "kawaii"}})

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]
    assert raw["display"]["personality"] == "kawaii"


def test_existing_unversioned_file_is_not_prematurely_promoted_by_writer(tmp_path):
    """Recovery, not an incidental edit, owns promotion of an old ambiguous file."""
    from hermes_cli.config import atomic_config_write

    config_path = tmp_path / "config.yaml"
    config_path.write_text("display:\n  personality: kawaii\n", encoding="utf-8")

    atomic_config_write(config_path, {"display": {"personality": "pirate"}})

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "_config_version" not in raw
    assert raw["display"]["personality"] == "pirate"


def test_unversioned_recovery_is_separate_from_historical_runner(monkeypatch):
    import hermes_cli.config_migrations as migrations

    calls = []

    def config_evidenced(_results, _quiet):
        calls.append("config-evidenced")

    def historical_only(_results, _quiet):
        calls.append("historical-only")

    config_evidenced = migrations._config_evidence_repair(config_evidenced)
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        ((1, config_evidenced), (2, historical_only)),
    )
    results = {"env_added": [], "config_added": [], "warnings": []}

    migrations.repair_unversioned_config(results, quiet=True)
    assert calls == ["config-evidenced"]

    calls.clear()
    migrations.run_migrations(0, results, quiet=True)
    assert calls == ["config-evidenced", "historical-only"]


def test_template_seed_declares_current_schema(tmp_path, monkeypatch):
    from hermes_cli.config import check_config_version

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    shutil.copy(TEMPLATE, tmp_path / "config.yaml")

    current, latest = check_config_version(raise_on_parse_error=True)
    assert current == latest

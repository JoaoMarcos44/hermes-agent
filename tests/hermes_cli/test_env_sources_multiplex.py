"""The resolver must not advertise .env files the loader skips (#19).

``load_hermes_dotenv`` returns an empty list without reading anything while a
multiplex gateway has a routed profile home installed: copying that profile's
``.env`` into the shared ``os.environ`` would leak its credentials to sibling
turns.  ``resolve_env_sources`` is documented as the single source of truth for
which files count and is what the reporting commands print from, so it has to
reach the same answer in that state.

These tests pin the anti-drift invariant *inside* the multiplex branch, which
``tests/hermes_cli/test_env_source_reporting.py`` only covers outside it.
"""

import pytest

from agent import secret_scope
from hermes_constants import set_hermes_home_override
from hermes_cli.env_loader import load_hermes_dotenv, resolve_env_sources


def _write_env(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def env_files(tmp_path):
    """A user .env and a project .env that both exist on disk."""
    home = tmp_path / "hermes"
    project_env = _write_env(tmp_path / "repo" / ".env", "PROJECT=1\n")
    _write_env(home / ".env", "USER=1\n")
    return home, project_env


@pytest.fixture
def multiplex(monkeypatch):
    """Enter/leave multiplex mode without leaking the flag into other tests."""
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    yield


@pytest.fixture
def routed_home(tmp_path):
    """Install a context-local routed profile home and reset it afterwards."""
    token = set_hermes_home_override(tmp_path / "routed-profile")
    yield
    from hermes_constants import _HERMES_HOME_OVERRIDE

    _HERMES_HOME_OVERRIDE.reset(token)


def test_resolver_reports_nothing_while_the_load_is_skipped(
    env_files, multiplex, routed_home
):
    """The bug: files exist on disk, but the loader is skipping them."""
    home, project_env = env_files

    assert resolve_env_sources(hermes_home=home, project_env=project_env) == []


def test_resolver_matches_the_loader_under_multiplex(
    env_files, multiplex, routed_home
):
    """The anti-drift invariant, made total: it must hold in this branch too."""
    home, project_env = env_files

    loaded = load_hermes_dotenv(
        hermes_home=home, project_env=project_env, load_external_secrets=False
    )

    assert loaded == resolve_env_sources(hermes_home=home, project_env=project_env)
    assert loaded == []


def test_unscoped_multiplex_load_still_resolves_normally(env_files, multiplex):
    """Preserved: multiplex without a routed home is an ordinary startup load."""
    home, project_env = env_files

    assert resolve_env_sources(hermes_home=home, project_env=project_env) == [
        home / ".env",
        project_env,
    ]


def test_routed_home_outside_multiplex_still_resolves_normally(
    env_files, routed_home
):
    """Preserved: an override alone (no multiplex) does not skip the load."""
    home, project_env = env_files

    assert resolve_env_sources(hermes_home=home, project_env=project_env) == [
        home / ".env",
        project_env,
    ]

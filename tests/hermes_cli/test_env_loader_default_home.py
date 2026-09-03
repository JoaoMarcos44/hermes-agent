"""The dotenv loader must resolve its default home the way everyone else does.

``load_hermes_dotenv`` and ``resolve_env_sources`` used to fall back to a bare
``os.getenv("HERMES_HOME", Path.home() / ".hermes")``.  That hard-codes the
POSIX layout, while ``get_hermes_home()`` / ``get_env_path()`` — and therefore
``hermes status`` and ``hermes doctor`` — resolve the platform-native default
(``%LOCALAPPDATA%\\hermes`` on Windows).  With ``HERMES_HOME`` unset on Windows
the startup load in ``hermes_cli/main.py`` read one file while both reporting
commands described another.

These tests pin the delegation itself rather than the Windows path, so they
assert the same invariant on every platform: with ``HERMES_HOME`` unset, the
loader reads whatever ``hermes_constants`` calls the platform default.
"""

import os

import hermes_constants
from hermes_cli.env_loader import load_hermes_dotenv, resolve_env_sources


def _write_env(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _pin_platform_default(monkeypatch, home):
    """Make the platform-native default resolve to ``home`` on any OS."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(
        hermes_constants, "_get_platform_default_hermes_home", lambda: home
    )


def test_resolver_follows_platform_default_home(tmp_path, monkeypatch):
    home = tmp_path / "platform-home"
    user_env = _write_env(home / ".env", "PLATFORM_DEFAULT=1\n")
    _pin_platform_default(monkeypatch, home)

    assert resolve_env_sources() == [user_env]


def test_loader_follows_platform_default_home(tmp_path, monkeypatch):
    """The regression: the startup load must read the file status/doctor report."""
    home = tmp_path / "platform-home"
    _write_env(home / ".env", "PLATFORM_DEFAULT_KEY=from-platform-home\n")
    _pin_platform_default(monkeypatch, home)
    monkeypatch.delenv("PLATFORM_DEFAULT_KEY", raising=False)

    loaded = load_hermes_dotenv(load_external_secrets=False)

    assert loaded == [home / ".env"]
    assert os.environ["PLATFORM_DEFAULT_KEY"] == "from-platform-home"


def test_loader_and_resolver_agree_on_the_default_home(tmp_path, monkeypatch):
    """Anti-drift: neither side may re-derive the home on its own."""
    home = tmp_path / "platform-home"
    _write_env(home / ".env", "AGREEMENT=1\n")
    _pin_platform_default(monkeypatch, home)

    assert load_hermes_dotenv(load_external_secrets=False) == resolve_env_sources()


def test_hermes_home_env_var_still_wins_over_platform_default(tmp_path, monkeypatch):
    """Behavior preserved: an explicit HERMES_HOME still beats the default."""
    platform_home = tmp_path / "platform-home"
    _write_env(platform_home / ".env", "WRONG_HOME=1\n")
    env_home = tmp_path / "env-home"
    env_var_env = _write_env(env_home / ".env", "RIGHT_HOME=1\n")
    monkeypatch.setattr(
        hermes_constants, "_get_platform_default_hermes_home", lambda: platform_home
    )
    monkeypatch.setenv("HERMES_HOME", str(env_home))

    assert resolve_env_sources() == [env_var_env]


def test_explicit_hermes_home_argument_still_wins(tmp_path, monkeypatch):
    """Behavior preserved: the keyword argument beats env var and default."""
    platform_home = tmp_path / "platform-home"
    _write_env(platform_home / ".env", "WRONG_HOME=1\n")
    explicit_home = tmp_path / "explicit-home"
    explicit_env = _write_env(explicit_home / ".env", "EXPLICIT=1\n")
    _pin_platform_default(monkeypatch, platform_home)

    assert resolve_env_sources(hermes_home=explicit_home) == [explicit_env]

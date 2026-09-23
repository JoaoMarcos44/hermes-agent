"""Portable db_path handling for the holographic memory fact store.

Single canonical home for every ``db_path`` decision so the CLI wizard, the
dashboard, profile clone/rename, and the runtime resolver cannot drift apart:

- the stored form is portable (``$HERMES_HOME/memory_store.db``), resolved per
  active profile at runtime — a cloned or renamed profile therefore keeps its
  own fact DB instead of sharing the source's or losing its facts;
- legacy concrete values (``~/.hermes/profiles/<name>/memory_store.db`` or an
  OS-absolute spelling) are recognised and healed back to the portable form.

No heavy imports: profile lifecycle code (``hermes_cli/profiles.py``) reuses
this module without importing the provider.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Portable stored form. Resolved against the ACTIVE profile on every read.
PORTABLE_DB_PATH = "$HERMES_HOME/memory_store.db"

#: Fact DB filename. Only values with this basename are treated as the default
#: store (and therefore portable); any other filename is a deliberate custom
#: path and is always respected verbatim.
DEFAULT_DB_FILENAME = "memory_store.db"


def _current_home_str(hermes_home: "str | os.PathLike[str] | None" = None) -> str:
    if hermes_home is not None:
        return str(hermes_home)
    from hermes_constants import get_hermes_home

    return str(get_hermes_home())


def expand_db_path(raw: object, hermes_home: "str | os.PathLike[str] | None" = None) -> str:
    """Expand a stored ``db_path`` to a concrete filesystem path.

    Handles the portable ``$HERMES_HOME`` / ``${HERMES_HOME}`` placeholders and
    ``~``. Empty/missing values fall back to the active profile default.
    """
    home = _current_home_str(hermes_home)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return str(Path(home) / DEFAULT_DB_FILENAME)
    text = str(raw)
    text = text.replace("$HERMES_HOME", home).replace("${HERMES_HOME}", home)
    return os.path.expanduser(text)


def _is_portable(value: object) -> bool:
    return isinstance(value, str) and "$HERMES_HOME" in value


def _expanded_equals_home_default(expanded: str, home: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(expanded)) == os.path.normcase(
            os.path.abspath(str(Path(home) / DEFAULT_DB_FILENAME))
        )
    except OSError:
        return False


def normalize_db_path_for_save(
    value: object, hermes_home: "str | os.PathLike[str] | None" = None
) -> str:
    """Normalize a ``db_path`` about to be persisted to ``config.yaml``.

    - Empty / missing -> portable default.
    - Already portable -> kept verbatim (canonical ``$HERMES_HOME`` spelling).
    - Anything resolving to ``<active-home>/memory_store.db`` (concrete
      ``~/...`` display default, OS-absolute spelling, ``~`` form) ->
      portable default, so clones/renames stay per-profile.
    - A path inside the active home with a custom filename ->
      ``$HERMES_HOME/<relative>`` portable spelling.
    - Anything else (custom absolute/relative path outside the home) ->
      respected verbatim (trimmed).
    """
    home = _current_home_str(hermes_home)
    if value is None or (isinstance(value, str) and not value.strip()):
        return PORTABLE_DB_PATH
    text = str(value).strip()
    if _is_portable(text):
        return PORTABLE_DB_PATH if expand_db_path(text, home) == str(
            Path(home) / DEFAULT_DB_FILENAME
        ) else text
    expanded = os.path.expanduser(text.replace("${HERMES_HOME}", home).replace("$HERMES_HOME", home))
    if _expanded_equals_home_default(expanded, home):
        return PORTABLE_DB_PATH
    try:
        rel = Path(expanded).resolve().relative_to(Path(home).resolve())
        return f"$HERMES_HOME/{rel.as_posix()}"
    except (OSError, ValueError):
        return text


def is_stale_profile_pinned_path(
    raw: object, hermes_home: "str | os.PathLike[str] | None" = None
) -> bool:
    """True when *raw* is a legacy concrete path pinning another profile's DB.

    A stale value is an absolute (or ``~``-rooted) path with the default
    filename that does NOT resolve inside the active home but DOES look like a
    profile DB: inside a ``profiles/<name>/`` tree, directly under a ``.hermes``
    home, or next to a profile identity marker (``config.yaml``/``.env``/
    ``SOUL.md``/``profile.yaml``). Custom filenames and paths outside any
    Hermes tree are intentional sharing/customisation and are never stale.
    """
    if raw is None or _is_portable(raw):
        return False
    text = str(raw).strip()
    if not text:
        return False
    home = _current_home_str(hermes_home)
    expanded = os.path.expanduser(text)
    if _expanded_equals_home_default(expanded, home):
        return False
    if Path(expanded).name != DEFAULT_DB_FILENAME:
        return False
    if not os.path.isabs(expanded):
        return False
    try:
        Path(expanded).resolve().relative_to(Path(home).resolve())
        return False  # inside the active home (custom spelling) — not stale
    except (OSError, ValueError):
        pass
    return _looks_like_profile_db(expanded)


def _looks_like_profile_db(expanded: str) -> bool:
    """True when an absolute default-named DB path lives in a profile-like tree."""
    try:
        parts = Path(expanded).parts
        lowered = [p.lower() for p in parts]
        if "profiles" in lowered and ".hermes" in lowered:
            return True
        if "profiles" in lowered:
            return True
        parent = Path(expanded).parent
        if parent.name == ".hermes":
            return True
        try:
            resolved_parent = Path(expanded).resolve().parent
        except OSError:
            resolved_parent = parent
        for marker in ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db"):
            try:
                if (resolved_parent / marker).exists():
                    return True
            except OSError:
                continue
        return False
    except (OSError, RuntimeError):
        return False


def resolve_db_path(
    raw: object, hermes_home: "str | os.PathLike[str] | None" = None
) -> str:
    """Resolve the effective concrete DB path for the active profile.

    Portable values expand against the active home. Legacy stale concrete
    values (another profile's DB) heal to the active profile's default so a
    cloned profile stops sharing its source's facts and a renamed profile
    finds its own facts again.
    """
    home = _current_home_str(hermes_home)
    if is_stale_profile_pinned_path(raw, home):
        return str(Path(home) / DEFAULT_DB_FILENAME)
    return expand_db_path(raw, home)

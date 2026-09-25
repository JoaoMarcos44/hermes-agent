"""Private, non-argv file delivery for remote execution environments."""

from __future__ import annotations

import logging
import posixpath
import re
import secrets
import shlex
import tempfile
from pathlib import Path
from typing import Any

from tools.spill_safety import ensure_spill_dir, write_text_exclusive

logger = logging.getLogger(__name__)
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
# The per-call env file exports the RPC pair (first two); a kernel env file also
# exports the remaining names. The private-env helper confines all of them to a
# launch subshell so pre-existing caller values are restored automatically.
RPC_KERNEL_ENV_NAMES = (
    "HERMES_RPC_DIR",
    "HERMES_RPC_TOKEN",
    "HERMES_KERNEL_DIR",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
)


def _checked_result(env: Any, command: str, *, timeout: int) -> dict:
    result = env.execute(command, cwd="/", timeout=timeout)
    if not isinstance(result, dict) or result.get("returncode") != 0:
        raise RuntimeError(f"remote private-file operation failed: {command.split()[0]}")
    return result


def create_private_remote_dir(env: Any, prefix: str, *, subdirs: tuple[str, ...] = ()) -> str:
    """Use plain mkdir so a name collision fails closed under the private umask."""
    if not _REMOTE_NAME.fullmatch(prefix) or any(not _REMOTE_NAME.fullmatch(name) for name in subdirs):
        raise ValueError("remote private directory names must be simple path components")
    get_temp_dir = getattr(env, "get_temp_dir", None)
    temp_dir = get_temp_dir() if callable(get_temp_dir) else None
    if not isinstance(temp_dir, str) or not temp_dir.startswith("/"):
        raise RuntimeError("remote backend has no absolute private scratch directory")
    temp_dir = posixpath.normpath(temp_dir)
    remote_dir = posixpath.join(temp_dir, f"{prefix}.{secrets.token_hex(8)}")
    if (posixpath.dirname(remote_dir) != temp_dir
            or not re.fullmatch(re.escape(f"{prefix}.") + r"[A-Za-z0-9_-]+", posixpath.basename(remote_dir))):
        raise RuntimeError("remote backend did not return a private scratch directory")
    _checked_result(env, f"umask 077 && mkdir -m 700 {shlex.quote(remote_dir)}", timeout=15)

    try:
        if subdirs:
            child_paths = [posixpath.join(remote_dir, name) for name in subdirs]
            _checked_result(
                env, "mkdir -m 700 " + " ".join(shlex.quote(path) for path in child_paths), timeout=15)
    except Exception:
        try:
            remove_private_remote_dir(env, remote_dir)
        except Exception:
            logger.debug("Remote private directory cleanup failed", exc_info=True)
        raise
    return remote_dir


def remove_private_remote_dir(env: Any, remote_dir: str) -> None:
    """Remove a directory created by :func:`create_private_remote_dir`."""
    _checked_result(env, f"rm -rf {shlex.quote(remote_dir)}", timeout=15)


def remote_owner_id(env: Any) -> str:
    """Return and cache the remote process UID used to namespace shared-temp results."""
    cached = getattr(env, "_hermes_remote_owner_id", None)
    if isinstance(cached, str) and cached.isdecimal():
        return cached
    result = _checked_result(env, "id -u", timeout=10)
    owner_id = next(
        (line.strip() for line in reversed((result.get("output") or "").splitlines())
         if line.strip().isdecimal()),
        None,
    )
    if owner_id is None:
        raise RuntimeError("remote backend did not return an OS user id")
    setattr(env, "_hermes_remote_owner_id", owner_id)
    return owner_id


def ensure_owner_scoped_results_dir(env: Any, temp_dir: str | None = None) -> str:
    """Create or tighten a private results directory scoped to the remote OS UID."""
    if temp_dir is None:
        get_temp_dir = getattr(env, "get_temp_dir", None)
        temp_dir = get_temp_dir() if callable(get_temp_dir) else None
    if not isinstance(temp_dir, str) or not temp_dir.startswith("/"):
        raise RuntimeError("remote backend has no absolute private scratch directory")
    temp_dir = posixpath.normpath(temp_dir)
    owner_id = remote_owner_id(env)
    results_dir = posixpath.join(temp_dir, f"hermes-results-{owner_id}")
    quoted = shlex.quote(results_dir)
    command = (
        f"mkdir -m 700 {quoted} 2>/dev/null || "
        f"{{ test -d {quoted} && test ! -L {quoted} && "
        f"_hermes_owner=$(stat -c %u {quoted} 2>/dev/null || stat -f %u {quoted} 2>/dev/null) && "
        f"test \"$_hermes_owner\" = {shlex.quote(owner_id)} && chmod 700 {quoted}; }}")
    _checked_result(env, command, timeout=15)
    return results_dir


def _upload_via_stdin(env: Any, remote_path: str, content: str) -> None:
    result = env.execute(
        f"umask 077 && cat > {shlex.quote(remote_path)}",
        cwd="/", timeout=30, stdin_data=content)
    if not isinstance(result, dict) or result.get("returncode") != 0:
        raise RuntimeError("remote stdin file delivery failed")


def _upload_via_file_sync(env: Any, remote_path: str, content: str) -> None:
    manager = getattr(env, "_sync_manager", None)
    upload_file = getattr(manager, "upload_file", None)
    if not callable(upload_file):
        raise RuntimeError("remote backend has no non-argv file upload channel")
    with tempfile.TemporaryDirectory(prefix="hermes-remote-file-") as staging:
        stage_dir = ensure_spill_dir(Path(staging))
        staged_file = stage_dir / "payload"
        write_text_exclusive(staged_file, content, newline="")
        upload_file(str(staged_file), remote_path)


def deliver_remote_file(env: Any, remote_path: str, content: str) -> None:
    """Upload UTF-8 content without placing it in command text, then enforce mode 0600."""
    uploaders = {
        "pipe": _upload_via_stdin,
        "payload": _upload_via_stdin,
        "heredoc": _upload_via_file_sync,
    }
    mode = getattr(env, "_stdin_mode", None)
    uploader = uploaders.get(mode)
    if uploader is None:
        raise RuntimeError(f"remote backend mode {mode!r} cannot deliver files without argv data")
    uploader(env, remote_path, content)
    _checked_result(env, f"chmod 600 {shlex.quote(remote_path)}", timeout=15)


def source_and_remove_env_file(remote_path: str, command: str, *, unset_names: tuple[str, ...]) -> str:
    """Run a child with a private env file in a subshell, preserving the caller's environment."""
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in unset_names):
        raise ValueError("environment cleanup names must be valid shell identifiers")
    quoted_path = shlex.quote(remote_path)
    return (
        "(\n"
        f"  . {quoted_path}\n"
        "  _hermes_source_status=$?\n"
        f"  rm -f {quoted_path}\n"
        "  _hermes_remove_status=$?\n"
        "  if [ $_hermes_source_status -ne 0 ]; then\n"
        "    exit $_hermes_source_status\n"
        "  fi\n"
        "  if [ $_hermes_remove_status -ne 0 ]; then\n"
        "    exit $_hermes_remove_status\n"
        "  fi\n"
        f"  {command}\n"
        "  _hermes_command_status=$?\n"
        "  exit $_hermes_command_status\n"
        ")")


def stage_remote_stdin(env: Any, content: str) -> tuple[str, str]:
    """Stage SDK-backend stdin in a private remote file; callers unlink it after opening stdin."""
    private_dir = create_private_remote_dir(env, "hermes-stdin")
    remote_file = posixpath.join(private_dir, "stdin")
    try:
        deliver_remote_file(env, remote_file, content)
    except Exception:
        try:
            remove_private_remote_dir(env, private_dir)
        except Exception:
            logger.debug("Remote stdin staging cleanup failed", exc_info=True)
        raise
    return private_dir, remote_file

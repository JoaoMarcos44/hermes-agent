"""A fresh launch finishes a source update using PM's success record, not a marker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import venv_sync
from pm.environments import runtime_facts_path


@pytest.fixture(autouse=True)
def _no_tool_downloads(monkeypatch):
    """The launch sync publishes lockfile tools first; these tests cover the sync decision."""
    import pm.client

    monkeypatch.setattr(pm.client, "ensure_tools_for_sync", lambda: None)


@pytest.fixture
def completion_tail(monkeypatch):
    """Record the source-completion child prepare_launch spawns after a sync instead of running it.

    The real child is ``hermes_cli/source_completion.py`` from the checkout under test — a
    scratch tree here — building products with the selected interpreter; the tests below
    cover the sync decision, not the build.
    """
    class Spawned(list):
        exit_code = 0
        kwargs: dict = {}

    spawned = Spawned()

    def call(command, **kwargs):
        spawned.append(command)
        spawned.kwargs = kwargs
        return spawned.exit_code

    monkeypatch.setattr(venv_sync.subprocess, "call", call)
    return spawned


def _self_checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='example'\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    return root


@pytest.mark.parametrize("argv", [["--version"], ["-V"], ["--help"], ["-p", "work", "-h"]])
def test_metadata_query_never_waits_on_source_completion(tmp_path, monkeypatch, argv):
    """`hermes --version` offline must answer from the tree, not run a network-bound sync."""
    import pm

    root = _self_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: pytest.fail("metadata query reached PM"))
    assert venv_sync.prepare_launch(root, argv) is None


def test_failed_completion_tail_is_retried_without_rebuilding_dependencies(tmp_path, monkeypatch, completion_tail):
    """Dependencies committed, tail failed: the next launch owes the tail only."""
    import pm
    from hermes_cli import _launchers

    root = _self_checkout(tmp_path, monkeypatch)
    fact = runtime_facts_path(root)
    syncs = []
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: fact.is_file())
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))

    def sync(extras=None, **kwargs):
        syncs.append(extras)
        fact.parent.mkdir(parents=True, exist_ok=True)
        fact.write_text(json.dumps({"packages": {"venv": {"stamp": "complete", "extras": ["all"]}}}))

    monkeypatch.setattr(pm, "sync_venv", sync)
    completion_tail.exit_code = 1
    with pytest.raises(RuntimeError, match="run `hermes update`"):
        venv_sync.prepare_launch(root, [])
    assert len(syncs) == 1 and len(completion_tail) == 1
    assert pm.venv_is_current()

    with pytest.raises(RuntimeError, match="run `hermes update`"):
        venv_sync.prepare_launch(root, [])
    assert len(syncs) == 1, "current dependencies were rebuilt for a tail retry"
    assert len(completion_tail) == 2

    completion_tail.exit_code = 0
    # Dependencies are already this interpreter's: the tail alone owes no re-exec.
    assert venv_sync.prepare_launch(root, []) is None
    assert len(syncs) == 1 and len(completion_tail) == 3
    assert venv_sync.prepare_launch(root, []) is None
    assert len(completion_tail) == 3, "a finished tail was run again"


def test_completion_tail_output_stays_off_stdout(tmp_path, monkeypatch, completion_tail):
    """The automatic tail runs in front of the user's command, which may be piping JSON."""
    import pm
    from hermes_cli import _launchers

    root = _self_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: None)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))
    venv_sync.prepare_launch(root, [])
    assert completion_tail.kwargs["stdout"] is sys.__stderr__


def test_first_launch_syncs_without_marker_then_uses_completion_fact(tmp_path, monkeypatch, completion_tail):
    import pm
    from hermes_cli import _launchers

    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='example'\n")
    (root / "uv.lock").write_text("lock\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    fact = runtime_facts_path(root)
    calls = []
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: fact.is_file())
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))

    def sync(extras=None, **kwargs):
        calls.append((extras, kwargs))
        fact.parent.mkdir(parents=True, exist_ok=True)
        fact.write_text(json.dumps({"packages": {"venv": {"stamp": "complete", "extras": ["all"]}}}))

    monkeypatch.setattr(pm, "sync_venv", sync)
    # A shipped updater may have written this before it reaches an inert shim.
    # It is obsolete after successful sync, not the trigger for that sync.
    (root / ".update-incomplete").write_text("pid=-1\n")
    assert venv_sync.prepare_launch(root, []) == Path(sys.executable)
    assert calls == [(["all"], {"explicit": True, "project_root": root, "evict_incompatible_plugins": True})]
    assert not (root / ".update-incomplete").exists()
    assert any("source_completion.py" in str(part) for cmd in completion_tail for part in cmd)
    assert venv_sync.prepare_launch(root, []) is None
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["script", "module", "command"])
def test_relaunch_keeps_invocation_and_checkout_imports(tmp_path, mode):
    root = tmp_path / "source"
    root.mkdir()
    (root / "checkout_only.py").write_text("value = 'from checkout'\n")
    script = root / "entry.py"
    script.write_text("import checkout_only, json, sys\nprint(json.dumps([checkout_only.value, sys.argv[1:]]))\n")
    argv = [str(script), "--profile", "name with spaces", "-c", "session"]
    orig = [sys.executable, *argv]
    module = None
    if mode == "module":
        module = "entry"
        orig = [sys.executable, "-m", module, *argv[1:]]
    elif mode == "command":
        argv[0] = "-c"
        orig = [sys.executable, "-c", "import entry", *argv[1:]]
    command = venv_sync.relaunch_command(Path(sys.executable), root, argv, orig, module)
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["from checkout", argv[1:]]


@pytest.mark.parametrize("owner,argv", [(None, []), ("external", []), ("electron-updater", []), ("self", ["-p", "coder", "pm", "repair"])])
def test_non_self_or_pm_launch_cannot_trigger_update(tmp_path, monkeypatch, owner, argv):
    import pm
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    if owner:
        (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": owner}))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: pytest.fail("unowned launch reached PM"))
    assert venv_sync.prepare_launch(root, argv) is None


def test_failed_launch_keeps_previous_completion_and_retries(tmp_path, monkeypatch):
    import pm
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    fact = runtime_facts_path(root)
    fact.parent.mkdir(parents=True)
    previous = '{"packages":{"venv":{"stamp":"previous","extras":["all","anthropic"]}}}'
    fact.write_text(previous)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)
    calls = []
    def fail(extras, **kwargs):
        calls.append(extras)
        raise RuntimeError("network unavailable")
    monkeypatch.setattr(pm, "sync_venv", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="network unavailable"):
            venv_sync.prepare_launch(root, [])
        assert fact.read_text() == previous
    assert calls == [None, None]
    assert not (root / ".update-incomplete").exists()


def test_blessed_legacy_install_is_adopted_before_sync(tmp_path, monkeypatch, completion_tail):
    import pm
    from hermes_cli import _launchers
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)
    calls = []
    monkeypatch.setattr(pm, "sync_venv", lambda *args, **kw: calls.append(args))
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))
    assert venv_sync.prepare_launch(root, []) == Path(sys.executable)
    assert json.loads((root / "install-stamp.json").read_text())["source"] == "adoption"
    assert calls == [(["all"],)]


def test_relaunch_runs_zip_launchers_and_preserves_interpreter_options(tmp_path):
    import zipfile
    launcher = tmp_path / "hermes.exe"
    with zipfile.ZipFile(launcher, "w") as archive:
        archive.writestr("__main__.py", "import json,sys; print(json.dumps([sys.argv[1:], sys.stdout.write_through, sys.flags.utf8_mode]))")
    original = [sys.executable, "-u", "-X", "utf8", str(launcher), "arg with spaces"]
    command = venv_sync.relaunch_command(Path(sys.executable), tmp_path, [str(launcher), "arg with spaces"], original, "__main__")
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [["arg with spaces"], True, 1]


def test_live_old_update_blocks_launch_sync(tmp_path, monkeypatch):
    import pm
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    marker = root / ".update-incomplete"
    marker.write_text(f"pid={os.getpid()}\n")
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: pytest.fail("raced old updater"))
    with pytest.raises(RuntimeError, match="still running"):
        venv_sync.prepare_launch(root, [])
    assert marker.is_file()
    # Fresh post-sync verification children may boot under a live updater.
    from hermes_cli import _launchers
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))
    assert venv_sync.prepare_launch(root, []) is None
    assert marker.is_file()


def test_launch_under_the_owning_update_does_not_run_the_tail_again(tmp_path, monkeypatch, completion_tail):
    """The tail imports the application, whose entry point runs prepare_launch: inside the
    process tree of the update that owns the pending tail it must be a no-op, not recurse."""
    import time
    import pm
    from hermes_cli.update_lock import update_marker_path

    root = _self_checkout(tmp_path, monkeypatch)
    pending = venv_sync.completion_pending_path(root)
    pending.parent.mkdir(parents=True)
    pending.write_text("owed\n")
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    marker = update_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{os.getppid()}\n{int(time.time())}\n")  # an ancestor holds the update

    assert venv_sync.prepare_launch(root, []) is None
    assert completion_tail == []
    assert pending.is_file(), "the owning update's obligation was discharged by its own tail"



def _arm_desktop_product(root: Path) -> None:
    release = root / "apps/desktop/release/win-unpacked"
    release.mkdir(parents=True, exist_ok=True)
    (release / "Hermes.exe").write_text("", encoding="utf-8")


def test_desktop_handoff_changes_only_marker_owner_and_preserves_acquisition_time(
    tmp_path, monkeypatch, completion_tail, capsys
):
    """PID ownership may move to Electron; the original stale ceiling may not."""
    import time
    from hermes_cli.update_lock import update_marker_path

    root = _self_checkout(tmp_path, monkeypatch)
    pending = venv_sync.completion_pending_path(root)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("owed\n", encoding="utf-8")
    _arm_desktop_product(root)
    parent_pid = 4242
    marker = update_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    started_at = int(time.time()) - 17
    marker.write_text(f"{os.getpid()}\n{started_at}\n", encoding="utf-8")
    published = []
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda published_root: published.append(published_root))

    with pytest.raises(SystemExit) as exc:
        venv_sync._finish_source_update(
            root,
            current=True,
            pending=pending,
            desktop_handoff_parent=parent_pid,
        )

    assert exc.value.code == venv_sync.DESKTOP_COMPLETION_HANDOFF_EXIT
    assert venv_sync.DESKTOP_COMPLETION_HANDOFF_SENTINEL in capsys.readouterr().err
    assert completion_tail == []
    assert published == [root]
    assert pending.is_file()
    assert marker.read_text(encoding="utf-8").splitlines() == [str(parent_pid), str(started_at)]


def test_desktop_handoff_falls_back_when_launcher_refresh_fails(
    tmp_path, monkeypatch, completion_tail
):
    """Never quit Desktop for a helper the durable launcher cannot start."""
    from hermes_cli.update_lock import update_marker_path

    root = _self_checkout(tmp_path, monkeypatch)
    pending = venv_sync.completion_pending_path(root)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("owed\n", encoding="utf-8")
    _arm_desktop_product(root)
    marker = update_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{os.getpid()}\n1\n", encoding="utf-8")

    def fail_publish(_root):
        raise RuntimeError("launcher unavailable")

    monkeypatch.setattr(venv_sync, "publish_launchers", fail_publish)

    venv_sync._finish_source_update(
        root,
        current=True,
        pending=pending,
        desktop_handoff_parent=4242,
    )
    assert len(completion_tail) == 1, "failed launcher refresh must retain #123510's ordinary tail"
    assert not pending.exists()


def test_shell_serve_never_uses_desktop_completion_handoff(tmp_path, monkeypatch, completion_tail):
    """#127296 regression: a terminal hermes serve must finish the ordinary tail itself."""
    import pm
    from hermes_cli import _launchers

    root = _self_checkout(tmp_path, monkeypatch)
    pending = venv_sync.completion_pending_path(root)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("owed\n", encoding="utf-8")
    (root / "apps/desktop/dist").mkdir(parents=True)
    (root / "apps/desktop/dist/index.html").write_text("", encoding="utf-8")
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_PARENT_PID", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_COMPLETION_HANDOFF", raising=False)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))

    assert venv_sync.prepare_launch(root, ["serve"]) is None
    assert len(completion_tail) == 1
    assert not pending.exists()


@pytest.mark.platforms("windows")
def test_windows_handoff_accepts_real_two_hop_packaged_ancestor(tmp_path, monkeypatch):
    """Exercise a real packaged-parent -> launcher -> worker ancestry chain on Windows."""
    import shutil

    root = _self_checkout(tmp_path, monkeypatch)
    script = root / "scripts/desktop-update/windows.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# fixture\n", encoding="utf-8")
    release = root / "apps/desktop/release/win-unpacked"
    release.mkdir(parents=True, exist_ok=True)
    desktop_exe = release / "Hermes.exe"
    comspec = os.environ.get("ComSpec") or shutil.which("cmd.exe")
    assert comspec, "Windows runner has no cmd.exe"
    shutil.copy2(comspec, desktop_exe)

    repo_root = Path(__file__).resolve().parents[2]
    worker = tmp_path / "handoff worker.py"
    worker.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from hermes_cli import venv_sync\n"
        "expected = int(os.environ['HERMES_PARENT_PID'])\n"
        "actual = venv_sync._desktop_completion_handoff_parent(Path(os.environ['HERMES_TEST_ROOT']))\n"
        "print(actual or 0)\n"
        "raise SystemExit(0 if actual == expected else 1)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "handoff launcher.py"
    launcher.write_text(
        "import os, subprocess, sys\n"
        "env = os.environ.copy()\n"
        "env['HERMES_PARENT_PID'] = str(os.getppid())\n"
        "result = subprocess.run([sys.executable, os.environ['HERMES_TEST_WORKER']], "
        "env=env, capture_output=True, text=True, timeout=30)\n"
        "sys.stdout.write(result.stdout)\n"
        "sys.stderr.write(result.stderr)\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["HERMES_DESKTOP"] = "1"
    env["HERMES_DESKTOP_COMPLETION_HANDOFF"] = "1"
    env["HERMES_TEST_ROOT"] = str(root)
    env["HERMES_TEST_WORKER"] = str(worker)
    env["PYTHONPATH"] = str(repo_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    command = subprocess.list2cmdline([sys.executable, str(launcher)])
    result = subprocess.run(
        [str(desktop_exe), "/d", "/s", "/c", command],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert int(result.stdout.strip()) > 0


@pytest.mark.platforms("windows")
def test_windows_completion_handoff_powershell_entry_preserves_started_at(tmp_path):
    """Native smoke test for the completion switch and the shared marker contract."""
    import shutil
    import time

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell, "Windows runner has no Windows PowerShell"
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts/desktop-update/windows.ps1"
    home = tmp_path / "Hermes Home"
    home.mkdir()
    started_at = int(time.time()) - 11
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_UPDATE_STARTED_AT"] = str(started_at)

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InstallRoot",
            str(repo_root),
            "-DesktopPid",
            "0",
            "-FinishPendingSourceCompletion",
            "-SelfTestMarker",
            "-NoUi",
            "-NoMarkerCleanup",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    marker = home / ".hermes-update-in-progress"
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert int(lines[0]) > 0
    assert lines[1] == str(started_at)


@pytest.mark.platforms("windows")
def test_windows_older_desktop_without_handoff_capability_keeps_123510_fallback(tmp_path, monkeypatch):
    root = _self_checkout(tmp_path, monkeypatch)
    script = root / "scripts/desktop-update/windows.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.delenv("HERMES_DESKTOP_COMPLETION_HANDOFF", raising=False)
    assert venv_sync._desktop_completion_handoff_parent(root) is None


@pytest.mark.platforms("windows")
def test_windows_desktop_env_without_release_ancestor_cannot_request_handoff(tmp_path, monkeypatch):
    """Inherited Desktop env is insufficient without real release ownership."""
    root = _self_checkout(tmp_path, monkeypatch)
    script = root / "scripts/desktop-update/windows.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.setenv("HERMES_DESKTOP_COMPLETION_HANDOFF", "1")
    monkeypatch.setenv("HERMES_PARENT_PID", str(os.getppid()))
    assert venv_sync._desktop_completion_handoff_parent(root) is None

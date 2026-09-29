"""Native invariant for the Windows completion-only Desktop hand-off."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.platforms("windows")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
WINDOWS_UPDATE_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


def _fixture(tmp_path: Path, *, exit_code: int) -> tuple[Path, Path, Path, dict[str, str]]:
    install_root = tmp_path / "checkout"
    launcher_dir = install_root / ".hermes" / "bin"
    launcher_dir.mkdir(parents=True)
    (install_root / "pm").mkdir()

    argv_file = tmp_path / "completion-argv.json"
    completion = tmp_path / "fake-source-completion.py"
    completion.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['HERMES_TEST_COMPLETION_ARGV']).write_text("
        "json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "raise SystemExit(int(os.environ.get('HERMES_TEST_COMPLETION_EXIT', '0')))\n",
        encoding="utf-8",
    )
    runtime = launcher_dir / "runtime.py"
    runtime.write_text(
        "import json, sys\n"
        f"print(json.dumps([sys.executable, {str(completion)!r}]))\n",
        encoding="utf-8",
    )
    (launcher_dir / "hermes.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{runtime}" %*\r\n',
        encoding="utf-8",
    )

    home = tmp_path / "home"
    home.mkdir()
    fallback = tmp_path / "install-state" / "source-completion-desktop-fallback"
    env = os.environ.copy()
    env.update(
        HERMES_HOME=str(home),
        HERMES_TEST_COMPLETION_ARGV=str(argv_file),
        HERMES_TEST_COMPLETION_EXIT=str(exit_code),
        TEMP=str(tmp_path / "temp"),
        TMP=str(tmp_path / "temp"),
    )
    (tmp_path / "temp").mkdir()
    return install_root, argv_file, fallback, env


@pytest.mark.parametrize(("exit_code", "fallback_expected"), [(0, False), (9, True)])
def test_completion_only_handoff_runs_tail_and_latches_safe_fallback(
    tmp_path: Path, exit_code: int, fallback_expected: bool
) -> None:
    install_root, argv_file, fallback, env = _fixture(tmp_path, exit_code=exit_code)
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows updater tests require Windows PowerShell."

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_UPDATE_PS1),
            "-InstallRoot",
            str(install_root),
            "-FinishSourceCompletion",
            "-SourceCompletionFallbackPath",
            str(fallback),
            "-NoUi",
        ],
        env=env,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
        check=False,
    )

    assert result.returncode == exit_code, result.stdout
    assert json.loads(argv_file.read_text(encoding="utf-8")) == [
        "--source",
        str(install_root),
        "--finish-update",
        "--desktop",
        "--clear-pending-on-success",
    ]
    assert fallback.exists() is fallback_expected

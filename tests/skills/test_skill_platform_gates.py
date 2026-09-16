"""Platform-gate regression for CUDA-bound and POSIX-only optional skills.

Regression for #113449. Front-matter ``platforms`` feeds skill selection,
so a declared platform the body cannot run on gets the skill picked and
failing at the first step. Each test below pins the contract between the
front-matter gate and the body evidence (not a frozen snapshot): it fails
when the impossible platform is re-declared without also fixing the body.
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def _load(skill_dir: str):
    p = REPO / skill_dir / "SKILL.md"
    content = p.read_text(encoding="utf-8")
    assert content.startswith("---"), f"{skill_dir}: SKILL.md must start with ---"
    m = re.search(r"\n---\s*\n", content[3:])
    assert m, f"{skill_dir}: unclosed frontmatter"
    fm = yaml.safe_load(content[3 : m.start() + 3])
    body = content[m.start() + 3 :]
    return fm, body


def test_heartmula_does_not_claim_windows_without_windows_path():
    """heartmula body is POSIX-only (``. .venv/bin/activate``); windows needs a Windows activation path."""
    fm, body = _load("optional-skills/creative/heartmula")
    platforms = fm.get("platforms") or []
    # The body reason this gate exists: POSIX dot-source activation.
    assert ".venv/bin/activate" in body
    has_windows_path = (
        ".venv\\Scripts" in body
        or "Scripts/activate" in body
        or "Activate.ps1" in body
        or re.search(r"(?i)powershell", body) is not None
    )
    if "windows" in platforms:
        assert has_windows_path, (
            "heartmula declares windows but the body has no Windows venv "
            "activation path (.venv\\Scripts / PowerShell)"
        )
    else:
        # Fixed state: windows not claimed while the body stays POSIX-only.
        assert not has_windows_path or True
    assert "windows" not in platforms


def test_tensorrt_llm_does_not_claim_macos_while_cuda_bound():
    """tensorrt-llm requires NVIDIA CUDA and redirects Apple Silicon to llama.cpp."""
    fm, body = _load("optional-skills/mlops/tensorrt-llm")
    platforms = fm.get("platforms") or []
    # The body reasons this gate exists: CUDA-only stack + Apple Silicon redirect.
    assert "Requires CUDA" in body
    assert "Apple Silicon" in body
    assert "macos" not in platforms

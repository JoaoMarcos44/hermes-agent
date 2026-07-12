#!/bin/bash
# Bootstraps token-optimization tooling for Claude Code on the web sessions:
#   - rtk        (github.com/rtk-ai/rtk)        global Bash-output compressor
#   - graphify   (github.com/Graphify-Labs/graphify) local codebase knowledge graph
#   - caveman    (github.com/JuliusBrussee/caveman)  terse-response Claude Code plugin
# Idempotent: safe to re-run. Installs are skipped when already present so a
# warm/cached container just re-applies config instead of rebuilding from source.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"

# ---------------------------------------------------------------------------
# rtk
# ---------------------------------------------------------------------------
if ! command -v rtk >/dev/null 2>&1; then
  if command -v cargo >/dev/null 2>&1; then
    cargo install --git https://github.com/rtk-ai/rtk --locked 2>&1 | tail -5 \
      || cargo install --git https://github.com/rtk-ai/rtk 2>&1 | tail -5
  else
    echo "[session-start] cargo not found, skipping rtk install" >&2
  fi
fi

if command -v rtk >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/rtk"
  cat > "$HOME/.config/rtk/config.toml" << 'RTKEOF'
[tracking]
enabled = true
history_days = 90

[display]
colors = true
emoji = false
max_width = 80

[filters]
ignore_dirs = [
    ".git",
    "node_modules",
    "target",
    "__pycache__",
    ".venv",
    "vendor",
]
ignore_files = [
    "*.lock",
    "*.min.js",
    "*.min.css",
]

[tee]
enabled = true
mode = "failures"
max_files = 20
max_file_size = 1048576

[telemetry]
enabled = false

[hooks]
exclude_commands = []
transparent_prefixes = []

[limits]
grep_max_results = 50
grep_max_per_file = 8
status_max_files = 8
status_max_untracked = 5
passthrough_max_chars = 500
RTKEOF
  rtk init -g --auto-patch --ultra-compact >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# graphify
# ---------------------------------------------------------------------------
if ! command -v graphify >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv tool install graphifyy 2>&1 | tail -5
  else
    echo "[session-start] uv not found, skipping graphify install" >&2
  fi
fi

if command -v graphify >/dev/null 2>&1; then
  graphify install --platform claude >/dev/null 2>&1 || true
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ ! -f "$CLAUDE_PROJECT_DIR/graphify-out/graph.json" ]; then
    (cd "$CLAUDE_PROJECT_DIR" && graphify extract . --code-only) || true
  fi
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
    (cd "$CLAUDE_PROJECT_DIR" && graphify hook install) >/dev/null 2>&1 || true
  fi
fi

# ---------------------------------------------------------------------------
# caveman
# ---------------------------------------------------------------------------
if ! command -v claude >/dev/null 2>&1 || ! claude plugin list 2>/dev/null | grep -q "caveman@caveman"; then
  if command -v node >/dev/null 2>&1; then
    curl -fsSL https://raw.githubusercontent.com/JuliusBrussee/caveman/main/install.sh | bash || true
  else
    echo "[session-start] node not found, skipping caveman install" >&2
  fi
fi

mkdir -p "$HOME/.config/caveman"
cat > "$HOME/.config/caveman/config.json" << 'CAVEOF'
{
  "defaultMode": "ultra"
}
CAVEOF

# ---------------------------------------------------------------------------
# Normalize ~/.claude/settings.json PreToolUse hooks: `rtk init -g` and
# `graphify install` each append their own matcher blocks on every run
# without deduplicating against each other (or against a prior run of this
# script), and `rtk init -g --ultra-compact` does not actually persist the
# flag into the hook command it writes. Collapse everything into one Bash
# entry and one Read|Glob entry with the exact commands we want, run last so
# it always wins regardless of what either installer just did.
# ---------------------------------------------------------------------------
python3 - << 'PYEOF'
import json, os

path = os.path.expanduser("~/.claude/settings.json")
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
else:
    data = {}

hooks = data.setdefault("hooks", {})
pretooluse = hooks.get("PreToolUse", [])

by_matcher = {}
for entry in pretooluse:
    m = entry.get("matcher")
    cmds = by_matcher.setdefault(m, [])
    for h in entry.get("hooks", []):
        c = h.get("command")
        if c:
            cmds.append(c)

def normalize_bash(cmds):
    out = []
    seen_rtk = False
    for c in cmds:
        if c.startswith("rtk hook claude"):
            if not seen_rtk:
                out.append("rtk hook claude --ultra-compact")
                seen_rtk = True
        elif c.endswith("graphify hook-guard search"):
            if "graphify hook-guard search" not in out:
                out.append("graphify hook-guard search")
        elif c not in out:
            out.append(c)
    return out

def normalize_readglob(cmds):
    out = []
    for c in cmds:
        if c.endswith("graphify hook-guard read"):
            if "graphify hook-guard read" not in out:
                out.append("graphify hook-guard read")
        elif c not in out:
            out.append(c)
    return out

new_pretooluse = []
if "Bash" in by_matcher:
    new_pretooluse.append({
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": c} for c in normalize_bash(by_matcher["Bash"])]
    })
if "Read|Glob" in by_matcher:
    new_pretooluse.append({
        "matcher": "Read|Glob",
        "hooks": [{"type": "command", "command": c} for c in normalize_readglob(by_matcher["Read|Glob"])]
    })
for m, cmds in by_matcher.items():
    if m not in ("Bash", "Read|Glob"):
        new_pretooluse.append({"matcher": m, "hooks": [{"type": "command", "command": c} for c in cmds]})

hooks["PreToolUse"] = new_pretooluse

with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF

echo "[session-start] rtk / graphify / caveman bootstrap complete"

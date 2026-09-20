"""@-reference expansion (``@file:``, ``@folder:``, ``@diff``, ``@git:``, ``@url:`` + plugin prefixes)."""

from __future__ import annotations

import asyncio
import inspect
import json
import mimetypes
import os
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from agent.model_metadata import estimate_tokens_rough
from hermes_cli._subprocess_compat import IS_WINDOWS, harden_git_argv, noninteractive_git_env, windows_hide_flags
from hermes_cli.sizefmt import format_bytes

# ── Plugin context-reference provider API ────────────────────────────────────

# --------------------------------------------------------------------------- Plugin context-reference
# provider API (Issue #26193) ---------------------------------------------------------------------------
BUILTIN_PREFIXES = frozenset({"diff", "staged", "file", "folder", "git", "url"})

_context_reference_providers: dict[str, "ContextReferenceProvider"] = {}


class ContextCompletionItem:
    """A single autocomplete result from a context reference provider."""

    __slots__ = ("text", "display", "meta")

    def __init__(self, text: str, display: str = "", meta: str = "") -> None:
        self.text = text
        self.display = display or text
        self.meta = meta


class ContextReferenceProvider(ABC):
    """Base class for plugin @-prefix providers, registered via ``PluginContext.register_context_reference()``."""

    prefix: str = ""  # e.g. "issue", "channel", "doc"
    description: str = ""  # shown in autocomplete meta column

    @abstractmethod
    async def autocomplete(self, query: str, *, limit: int = 10) -> list[ContextCompletionItem]:
        """Return autocomplete items for the given query string."""

    @abstractmethod
    async def expand(self, target: str) -> str | None:
        """Expand *target* to prompt content.  Return ``None`` to skip."""


def register_context_reference_provider(provider: ContextReferenceProvider) -> None:
    """Register a plugin context reference provider."""
    if not isinstance(provider, ContextReferenceProvider):
        raise TypeError("provider must be a ContextReferenceProvider instance")
    prefix = provider.prefix.lower().strip()
    if not prefix:
        raise ValueError("prefix must be a non-empty string")
    if prefix in BUILTIN_PREFIXES:
        raise ValueError(f"prefix '{prefix}' is reserved for built-in references")
    if prefix in _context_reference_providers:
        raise ValueError(f"prefix '{prefix}' is already registered")
    _context_reference_providers[prefix] = provider


def get_context_reference_providers() -> dict[str, ContextReferenceProvider]:
    """Return a snapshot of all registered plugin providers."""
    return dict(_context_reference_providers)


_QUOTED_REFERENCE_VALUE = r'(?:`[^`\n]+`|"[^"\n]+"|\'[^\'\n]+\')'
REFERENCE_PATTERN = re.compile(
    rf"(?<![\w/])@(?:(?P<simple>diff|staged)\b|(?P<kind>file|folder|git|url):(?P<value>{_QUOTED_REFERENCE_VALUE}(?::\d+(?:-\d+)?)?|\S+))"
)
# Plugin fallback: any @<word>:<value> the built-in regex did not claim.
_PLUGIN_REFERENCE_PATTERN = re.compile(
    rf"(?<![\w/])@(?P<kind>[a-zA-Z][a-zA-Z0-9_-]*):(?P<value>{_QUOTED_REFERENCE_VALUE}(?::\d+(?:-\d+)?)?|\S+)"
)
# ``@file:`` value: quoted path or bare path, each with an optional ``:start[-end]`` range.
_FILE_VALUE_PATTERN = re.compile(
    r'^(?:(?P<quote>`|"|\')(?P<qpath>.+?)(?P=quote)|(?P<path>.+?))(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$'
)

TRAILING_PUNCTUATION = ",.;!?"
_OPENERS = {")": "(", "]": "[", "}": "{"}
_NEEDS_QUOTING = re.compile(r"""[\s()\[\]{}<>"'`]""")
_SENSITIVE_HOME_DIRS = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh")
_SENSITIVE_HERMES_DIRS = (Path("skills") / ".hub",)
_SENSITIVE_HOME_FILES = tuple(Path(p) for p in (
    ".ssh/authorized_keys", ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/config", ".bashrc", ".zshrc",
    ".profile", ".bash_profile", ".zprofile", ".netrc", ".pgpass", ".npmrc", ".pypirc",
))
_TEXT_EXTENSIONS = (".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".js", ".ts")
# Resource bound for @-reference expansion (#116562): every expander gates on
# bytes BEFORE materializing content, so a short remote message cannot force
# GB-scale transient allocations. The single budget below feeds all branches —
# file reads, subprocess output, and fetched/plugin text — instead of one ad
# hoc check per call site.
_SNIFF_BYTES = 4096
_INLINE_BYTES_SLACK = 512  # fence/header overhead on top of tokens * chars-per-token
_MAX_REFS_PER_MESSAGE = 32  # bounds asyncio.gather fan-out for hostile messages
_FOLDER_METADATA_BYTES = 256 * 1024  # larger files report size only, never line-counted
_READ_CHUNK_CHARS = 64 * 1024
_FENCE_LANGUAGES = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".json": "json", ".md": "markdown", ".sh": "bash", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
}


@dataclass(frozen=True)
class ContextReference:
    raw: str
    kind: str
    target: str
    start: int
    end: int
    line_start: int | None = None
    line_end: int | None = None


@dataclass
class ContextReferenceResult:
    message: str
    original_message: str
    references: list[ContextReference] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    injected_tokens: int = 0
    expanded: bool = False
    blocked: bool = False


UrlFetcher = Callable[[str], str | Awaitable[str]] | None
Expansion = tuple[str | None, str | None]  # (warning, block) — exactly one side is set


def format_reference_value(value: str) -> str:
    """Quote a value so ``REFERENCE_PATTERN`` (bare alternative ``\\S+``) reads it back whole.
    Mirrors ``formatRefValue`` in the desktop's directive-text.tsx."""
    if not _NEEDS_QUOTING.search(value):
        return value
    for quote in ("`", '"', "'"):
        if quote not in value:
            return f"{quote}{value}{quote}"
    return value


def parse_context_references(message: str) -> list[ContextReference]:
    refs: list[ContextReference] = []
    if not message:
        return refs
    for match in REFERENCE_PATTERN.finditer(message):
        kind = match.group("simple") or match.group("kind")
        value = _strip_trailing_punctuation(match.group("value") or "")
        if match.group("simple"):
            target, line_start, line_end = "", None, None
        elif kind == "file":
            target, line_start, line_end = _parse_file_reference_value(value)
        else:
            target, line_start, line_end = _strip_reference_wrappers(value), None, None
        refs.append(ContextReference(match.group(0), kind, target, match.start(), match.end(), line_start, line_end))

    # Second pass: plugin-registered prefixes the built-in pattern missed.
    for match in _PLUGIN_REFERENCE_PATTERN.finditer(message) if _context_reference_providers else ():
        kind = match.group("kind")
        if kind in BUILTIN_PREFIXES or kind not in _context_reference_providers:
            continue
        if any(r.kind == kind and r.start == match.start() for r in refs):
            continue
        target = _strip_reference_wrappers(_strip_trailing_punctuation(match.group("value") or ""))
        refs.append(ContextReference(match.group(0), kind, target, match.start(), match.end()))
    return refs


def preprocess_context_references(
    message: str, *, cwd: str | Path, context_length: int, url_fetcher: UrlFetcher = None,
    allowed_root: str | Path | None = None,
) -> ContextReferenceResult:
    """Sync wrapper; safe both without a loop (CLI) and inside a running loop (gateway)."""
    coro = preprocess_context_references_async(
        message, cwd=cwd, context_length=context_length, url_fetcher=url_fetcher, allowed_root=allowed_root
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    import contextvars
    # The side thread starts with an empty Context: without the caller's copy the served profile's
    # HERMES_HOME override is lost and the credential-path guard checks the launch profile's .env.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(contextvars.copy_context().run, asyncio.run, coro).result()


async def preprocess_context_references_async(
    message: str, *, cwd: str | Path, context_length: int, url_fetcher: UrlFetcher = None,
    allowed_root: str | Path | None = None,
) -> ContextReferenceResult:
    refs = parse_context_references(message)
    if not refs:
        return ContextReferenceResult(message=message, original_message=message)
    if len(refs) > _MAX_REFS_PER_MESSAGE:
        dropped = len(refs) - _MAX_REFS_PER_MESSAGE
        refs = refs[:_MAX_REFS_PER_MESSAGE]
        truncated_refs_warning = (
            f"@ context refs truncated: {dropped} reference(s) ignored "
            f"(limit {_MAX_REFS_PER_MESSAGE} per message)."
        )
    else:
        truncated_refs_warning = None
    cwd_path = Path(cwd).expanduser().resolve()
    # Default root = cwd so @ references cannot escape the workspace unless a caller widens it.
    allowed_root_path = Path(allowed_root).expanduser().resolve() if allowed_root is not None else cwd_path
    # Expand concurrently (each ref is independent; several @url: refs would otherwise
    # serialize web_extract round-trips). gather preserves order, so warnings/blocks
    # are assembled in ref order; the token-budget check runs once afterwards.
    hard_limit = max(1, int(context_length * 0.50))
    soft_limit = max(1, int(context_length * 0.25))
    tasks = (
        _expand_reference(ref, cwd_path, url_fetcher=url_fetcher, allowed_root=allowed_root_path,
                          max_inline_tokens=hard_limit)
        for ref in refs
    )
    expanded = await asyncio.gather(*tasks)
    warnings = [warning for warning, _ in expanded if warning]
    blocks = [block for _, block in expanded if block]
    if truncated_refs_warning is not None:
        warnings.append(truncated_refs_warning)
    injected_tokens = sum(estimate_tokens_rough(block) for block in blocks)
    result = ContextReferenceResult(
        message=message, original_message=message, references=refs, warnings=warnings, injected_tokens=injected_tokens
    )

    if injected_tokens > hard_limit:
        warnings.append(f"@ context injection refused: {injected_tokens} tokens exceeds the 50% hard limit ({hard_limit}).")
        result.blocked = True
        return result
    if injected_tokens > soft_limit:
        warnings.append(f"@ context injection warning: {injected_tokens} tokens exceeds the 25% soft limit ({soft_limit}).")

    # The `@file:`/`@folder:` tokens stay where the user typed them: the token IS the
    # reference (clients render it as an inline chip); stripping it left a hole in the
    # sentence and forced the desktop to re-derive refs from the attached block.
    final = message
    if warnings:
        final = f"{final}\n\n--- Context Warnings ---\n" + "\n".join(f"- {warning}" for warning in warnings)
    if blocks:
        final = f"{final}\n\n--- Attached Context ---\n\n" + "\n\n".join(blocks)
    result.message = final.strip()
    result.expanded = bool(blocks or warnings)
    return result


# Git-backed reference kinds -> f(ref) -> git argv (the label is "git " + argv).
_GIT_REFERENCE_ARGS: dict[str, Callable[[ContextReference], list[str]]] = {
    "diff": lambda ref: ["diff"],
    "staged": lambda ref: ["diff", "--staged"],
    "git": lambda ref: ["log", f"-{max(1, min(int(ref.target or '1'), 10))}", "-p"],
}


def _byte_budget_for_tokens(max_inline_tokens: int | None) -> int | None:
    """Single byte budget behind every @-reference size gate.

    Token estimates run ~4 bytes/token, so the budget is checked with cheap
    ``st_size`` / ``len()`` comparisons BEFORE any content is materialized.
    """
    if max_inline_tokens is None:
        return None
    from agent.model_metadata import CHARS_PER_TOKEN
    return max_inline_tokens * CHARS_PER_TOKEN + _INLINE_BYTES_SLACK


def _oversized_inline_reference_block(ref: ContextReference, label: str, approx_tokens: int,
                                      guidance: str) -> str:
    """Shared 📎 stub for refs with no on-disk path (diff / url / plugin).

    The content is not inlined; the model gets guidance for fetching a
    narrower slice with its own tools. One helper serves all branches.
    """
    return (
        f"📎 {ref.raw} (approximately {approx_tokens} tokens) — too large to inline safely. "
        f"{label} {guidance}"
    )


def _inline_over_budget(text: str, max_inline_tokens: int | None) -> int | None:
    """Return approx tokens when *text* exceeds the inline budget, else None.

    Length is checked first so oversized payloads never pay a full token scan.
    """
    if max_inline_tokens is None:
        return None
    budget = _byte_budget_for_tokens(max_inline_tokens)
    assert budget is not None
    if len(text) <= budget:
        tokens = estimate_tokens_rough(text)
        return tokens if tokens > max_inline_tokens else None
    return max(estimate_tokens_rough(text[:_SNIFF_BYTES]), (len(text) + 3) // 4)


async def _expand_reference(
    ref: ContextReference, cwd: Path, *, url_fetcher: UrlFetcher = None, allowed_root: Path | None = None,
    max_inline_tokens: int | None = None,
) -> Expansion:
    try:
        if ref.kind in ("file", "folder"):
            return _expand_path_reference(ref, cwd, allowed_root=allowed_root, max_inline_tokens=max_inline_tokens)
        if ref.kind in _GIT_REFERENCE_ARGS:
            git_args = _GIT_REFERENCE_ARGS[ref.kind](ref)
            return _expand_git_reference(ref, cwd, git_args, "git " + " ".join(git_args),
                                         max_inline_tokens=max_inline_tokens)
        if ref.kind == "url":
            content = await _fetch_url_content(ref.target, url_fetcher=url_fetcher)
            if not content:
                return f"{ref.raw}: no content extracted", None
            over = _inline_over_budget(content, max_inline_tokens)
            if over is not None:
                return None, _oversized_inline_reference_block(
                    ref, "Web content not inlined.",
                    over, "Use web_search or web_extract with a narrower query to fetch only the relevant section.")
            return None, f"🌐 {ref.raw} ({estimate_tokens_rough(content)} tokens)\n{content}"
    except Exception as exc:
        return f"{ref.raw}: {exc}", None
    provider = _context_reference_providers.get(ref.kind)
    if provider is not None:
        try:
            plugin_content = await provider.expand(ref.target)
            if plugin_content is not None:
                over = _inline_over_budget(plugin_content, max_inline_tokens)
                if over is not None:
                    return None, _oversized_inline_reference_block(
                        ref, "Plugin content not inlined.",
                        over, "Query the provider with a narrower target instead of loading the full result.")
                return None, f"📌 {ref.raw} ({estimate_tokens_rough(plugin_content)} tokens)\n{plugin_content}"
        except Exception as exc:
            return f"{ref.raw}: plugin expansion error: {exc}", None
    return f"{ref.raw}: unsupported reference type", None


def _read_text_bounded(path: Path, byte_budget: int | None) -> str | None:
    """Read a text file without ever holding more than the budget in memory.

    Returns None when the content exceeds *byte_budget* (caller emits the
    oversized stub). The pre-stat size check in the caller handles the common
    case; this streaming read closes the stat-to-read race where the file grows
    between the two.
    """
    if byte_budget is None:
        return path.read_text(encoding="utf-8", errors="replace")
    chunks: list[str] = []
    total = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK_CHARS)
            if not chunk:
                break
            total += len(chunk)
            if total > byte_budget:
                return None
            chunks.append(chunk)
    return "".join(chunks)


def _read_text_range_bounded(path: Path, start: int, end: int,
                             byte_budget: int | None) -> str | None:
    """Serve ``@file:x:start-end`` by streaming to the last needed line only.

    The rest of the file is never read, so a 500MB log with ``:1-5`` costs
    five lines of I/O instead of a full load plus slice.
    """
    lines: list[str] = []
    total = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for lineno, line in enumerate(handle, 1):
            if lineno < start:
                continue
            if lineno > end:
                break
            stripped = line.rstrip("\n")
            total += len(stripped) + 1
            if byte_budget is not None and total > byte_budget + _INLINE_BYTES_SLACK:
                return None
            lines.append(stripped)
    return "\n".join(lines)


def _expand_path_reference(ref: ContextReference, cwd: Path, *, allowed_root: Path | None = None,
                           max_inline_tokens: int | None = None) -> Expansion:
    """``@file:`` / ``@folder:``: resolve, allow-check, then inline text / binary stub / listing."""
    is_folder = ref.kind == "folder"
    path = _resolve_path(cwd, ref.target, allowed_root=allowed_root)
    _ensure_reference_path_allowed(path)
    if not path.exists():
        return f"{ref.raw}: {ref.kind} not found", None
    if not (path.is_dir() if is_folder else path.is_file()):
        return f"{ref.raw}: path is not a {ref.kind}", None
    if is_folder:
        listing = _build_folder_listing(path, cwd)
        return None, f"📁 {ref.raw} ({estimate_tokens_rough(listing)} tokens)\n{listing}"
    if _is_binary_file(path):
        # A bare "not supported" warning was a dead end (the model gave up); the file IS
        # on disk where the agent's tools run, so hand it an actionable block instead.
        return None, _binary_reference_block(ref, path)
    budget = _byte_budget_for_tokens(max_inline_tokens)
    if ref.line_start is not None:
        start = max(ref.line_start, 1)
        end = ref.line_end or ref.line_start
        text = _read_text_range_bounded(path, start, end, budget)
        if text is None:
            over = max(max_inline_tokens or 0, (end - start + 1) * 8)
            return None, _oversized_text_reference_block(ref, path, over)
    else:
        # Size-gate BEFORE the read: an oversized file produces the stub without
        # ever being loaded and token-scanned into memory.
        try:
            if budget is not None and path.stat().st_size > budget:
                over = max(max_inline_tokens or 0, (path.stat().st_size + 3) // 4)
                return None, _oversized_text_reference_block(ref, path, over)
        except OSError:
            pass
        text = _read_text_bounded(path, budget)
        if text is None:
            over = max_inline_tokens or 0
            try:
                over = max(over, (path.stat().st_size + 3) // 4)
            except OSError:
                pass
            return None, _oversized_text_reference_block(ref, path, over)
    lang = _FENCE_LANGUAGES.get(path.suffix.lower(), "")
    text_tokens = estimate_tokens_rough(text)
    # Check BEFORE building the fenced block: an oversized file is not going to be
    # inlined, so don't build a second MB-scale string just to discard it.
    if max_inline_tokens is not None and text_tokens > max_inline_tokens:
        # One oversized file used to poison the aggregate check and refuse the whole
        # turn (#61987); the file stays readable via the agent's tools instead. The
        # block alone carries the message (same shape as the binary path) — a warning
        # would duplicate it in "--- Context Warnings ---".
        return None, _oversized_text_reference_block(ref, path, text_tokens)
    return None, f"📄 {ref.raw} ({text_tokens} tokens)\n```{lang}\n{text}\n```"


def _run_quiet(cmd: list[str], cwd: Path, timeout: int, env: dict | None = None,
               max_output_bytes: int | None = None) -> subprocess.CompletedProcess:
    """Captured subprocess output with an optional streaming byte cap.

    Without a cap this behaves exactly as before. With a cap, stdout/stderr are
    streamed in chunks and the child is killed once the cap is passed, so
    ``git diff`` on a huge tree never buffers GBs before the token gate runs.
    Callers detect truncation via ``len(result.stdout) > max_output_bytes``.
    """
    popen_kwargs: dict = {"creationflags": windows_hide_flags()} if IS_WINDOWS else {}
    if max_output_bytes is None:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, stdin=subprocess.DEVNULL, **popen_kwargs, **({} if env is None else {"env": env}))
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                            **popen_kwargs, **({} if env is None else {"env": env}))
    try:
        import threading as _threading
        import time as _time
        chunks: dict[str, list[str]] = {"out": [], "err": []}
        sizes = {"out": 0, "err": 0}
        # Thread-per-pipe drain: portable across Windows and POSIX (select(2)
        # cannot wait on pipes on Windows). Each reader stops appending once
        # its cap is passed; stdout additionally kills the child so a huge
        # diff never finishes buffering GBs.
        stop = _threading.Event()

        def _drain(stream_name: str, cap: int) -> None:
            stream = proc.stdout if stream_name == "out" else proc.stderr
            assert stream is not None
            while not stop.is_set():
                data = stream.read(_READ_CHUNK_CHARS)
                if not data:
                    break
                if sizes[stream_name] <= cap:
                    chunks[stream_name].append(data)
                    sizes[stream_name] += len(data)
                if stream_name == "out" and sizes["out"] > max_output_bytes + _INLINE_BYTES_SLACK:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    break

        readers = [
            _threading.Thread(target=_drain, args=("out", max_output_bytes), daemon=True),
            _threading.Thread(target=_drain, args=("err", _SNIFF_BYTES), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop.set()
            proc.kill()
            raise subprocess.TimeoutExpired(cmd, timeout)
        deadline = _time.monotonic() + 5
        for reader in readers:
            remaining = deadline - _time.monotonic()
            reader.join(timeout=max(0.1, remaining))
        return subprocess.CompletedProcess(cmd, returncode, "".join(chunks["out"]), "".join(chunks["err"]))
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


def _expand_git_reference(ref: ContextReference, cwd: Path, args: list[str], label: str,
                          max_inline_tokens: int | None = None) -> Expansion:
    budget = _byte_budget_for_tokens(max_inline_tokens)
    try:
        # Repo-supplied config/attributes must never execute code (GHSA-7x36-8jrh-v4pw).
        result = _run_quiet(["git", *harden_git_argv(args)], cwd, 30, env=noninteractive_git_env(),
                            max_output_bytes=budget)
    except subprocess.TimeoutExpired:
        return f"{ref.raw}: git command timed out (30s)", None
    if result.returncode != 0:
        return f"{ref.raw}: {(result.stderr or '').strip() or 'git command failed'}", None
    if budget is not None and len(result.stdout) > budget:
        over = max(max_inline_tokens or 0, (len(result.stdout) + 3) // 4)
        return None, _oversized_inline_reference_block(
            ref, f"{label} not inlined.",
            over, "Use git with a narrower scope (git diff --stat, git diff -- <path>) to inspect only the relevant part.")
    content = result.stdout.strip() or "(no output)"
    over = _inline_over_budget(content, max_inline_tokens)
    if over is not None:
        return None, _oversized_inline_reference_block(
            ref, f"{label} not inlined.",
            over, "Use git with a narrower scope (git diff --stat, git diff -- <path>) to inspect only the relevant part.")
    return None, f"🧾 {label} ({estimate_tokens_rough(content)} tokens)\n```diff\n{content}\n```"


async def _fetch_url_content(url: str, *, url_fetcher: UrlFetcher = None) -> str:
    content = (url_fetcher or _default_url_fetcher)(url)
    if inspect.isawaitable(content):
        content = await content
    return str(content or "").strip()


async def _default_url_fetcher(url: str) -> str:
    from tools.web_tools import web_extract_tool
    docs = json.loads(await web_extract_tool([url], format="markdown")).get("results", [])
    return str(docs[0].get("content") or docs[0].get("raw_content") or "").strip() if docs else ""


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_path(cwd: Path, target: str, *, allowed_root: Path | None = None) -> Path:
    from agent.file_safety import is_nt_namespace_path
    if is_nt_namespace_path(target):  # raw-string check: resolving such a path is the NTLM-leak trigger
        raise ValueError("path uses a Windows NT/device namespace prefix and cannot be attached")
    resolved = (cwd / Path(os.path.expanduser(target))).resolve()  # `/` keeps an absolute target as-is
    if allowed_root is not None and not _is_under(resolved, allowed_root):
        raise ValueError("path is outside the allowed workspace")
    return resolved


def _ensure_reference_path_allowed(path: Path) -> None:
    """Refuse credential/internal paths. Fails CLOSED: the gateway feeds untrusted remote text here."""
    from hermes_constants import get_hermes_home
    home, hermes_home = Path(os.path.expanduser("~")).resolve(), get_hermes_home().resolve()
    blocked_exact = {home / rel for rel in _SENSITIVE_HOME_FILES} | {hermes_home / ".env"}
    blocked_dirs = [home / rel for rel in _SENSITIVE_HOME_DIRS] + [hermes_home / rel for rel in _SENSITIVE_HERMES_DIRS]
    if path in blocked_exact:
        raise ValueError("path is a sensitive credential file and cannot be attached")
    if any(_is_under(path, blocked_dir) for blocked_dir in blocked_dirs):
        raise ValueError("path is a sensitive credential or internal Hermes path and cannot be attached")
    # Anchor to the canonical read deny-list (agent/file_safety.get_read_block_error): the
    # narrow list above never caught auth.json, .anthropic_oauth.json, mcp-tokens/, webhook
    # secrets or project .env files, and it grows automatically with that deny-list.
    try:
        from agent.file_safety import get_read_block_error
        blocked = get_read_block_error(str(path)) is not None
    except ValueError:
        raise
    except Exception:
        # If the canonical lookup fails, falling through would re-open the exact hole this
        # guard closes; a spurious block is recoverable, a leaked credential is not.
        raise ValueError("path could not be verified against the credential deny-list and cannot be attached")
    if blocked:
        raise ValueError("path is a sensitive credential or internal Hermes path and cannot be attached")


def _strip_trailing_punctuation(value: str) -> str:
    stripped = value.rstrip(TRAILING_PUNCTUATION)
    # Drop unbalanced closers so "(see @file:x.py)" does not swallow the ")".
    while stripped.endswith((")", "]", "}")) and stripped.count(stripped[-1]) > stripped.count(_OPENERS[stripped[-1]]):
        stripped = stripped[:-1]
    return stripped


def _strip_reference_wrappers(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"'" else value


def _parse_file_reference_value(value: str) -> tuple[str, int | None, int | None]:
    m = _FILE_VALUE_PATTERN.match(value)
    start = m and m.group("start")
    if not start:  # no line range: the whole value is the (possibly quoted) path
        return _strip_reference_wrappers(value), None, None
    return m.group("qpath") or m.group("path"), int(start), int(m.group("end") or start)


def _is_binary_file(path: Path) -> bool:
    mime = mimetypes.guess_type(path.name)[0]
    if bool(mime and not mime.startswith("text/") and not path.name.endswith(_TEXT_EXTENSIONS)):
        return True
    # Sniff only the head: the old ``path.read_bytes()[:4096]`` loaded the
    # whole file to inspect the first 4KB.
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_SNIFF_BYTES)
    except OSError:
        return True


def _build_folder_listing(path: Path, cwd: Path, limit: int = 200) -> str:
    lines = [f"{path.relative_to(cwd)}/"]
    entries = _iter_visible_entries(path, cwd, limit=limit)
    base_depth = len(path.relative_to(cwd).parts)
    for entry in entries:
        indent = "  " * max(len(entry.relative_to(cwd).parts) - base_depth - 1, 0)
        lines.append(f"{indent}- {entry.name}/" if entry.is_dir() else f"{indent}- {entry.name} ({_file_metadata(entry)})")
    if len(entries) >= limit:
        lines.append("- ...")
    return "\n".join(lines)


def _iter_visible_entries(path: Path, cwd: Path, limit: int) -> list[Path]:
    """Files under ``path`` via ``rg --files`` (honours ignore files), else an os.walk fallback."""
    try:
        # Cap rg output: only ``limit`` lines are consumed, so a huge tree
        # never buffers its full file list before the slice below runs.
        rg = _run_quiet(["rg", "--files", str(path.relative_to(cwd))], cwd, 10,
                        max_output_bytes=limit * 512)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        rg = None
    if rg is not None and rg.returncode == 0:
        output: list[Path] = []
        seen_dirs: set[Path] = set()
        for line in [ln.strip() for ln in rg.stdout.splitlines() if ln.strip()][:limit]:
            full = cwd / Path(line)
            for parent in full.parents:
                if parent == cwd or parent in seen_dirs or path not in {parent, *parent.parents}:
                    continue
                seen_dirs.add(parent)
                output.append(parent)
            output.append(full)
        return sorted({p for p in output if p.exists()}, key=lambda p: (not p.is_dir(), str(p)))
    output = []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        files = sorted(f for f in files if not f.startswith("."))
        for name in dirs + files:
            output.append(Path(root) / name)
            if len(output) >= limit:
                return output
    return output


def _agent_visible_path(path: Path) -> str:
    # Under a container backend the host path dangles inside the sandbox: translate staged
    # files to their auto-mounted cache path; fall back to the host path (local backend /
    # translation failure). Run the idempotent TERMINAL_ENV bridge first so in-process
    # gateways that never bridged terminal.* config still see the active backend.
    try:
        from tools.terminal_tool import _ensure_terminal_env_bridged
        _ensure_terminal_env_bridged()
        from tools.credential_files import to_agent_visible_cache_path
        return to_agent_visible_cache_path(str(path))
    except Exception:
        return str(path)


def _on_disk_reference_block(ref: ContextReference, path: Path, descriptor: str, reason: str, guidance: str) -> str:
    """Shared 📎 shape: the file was not inlined, but it IS on disk where the agent's
    tools run — hand the model the path and a nudge instead of a dead-end warning."""
    try:
        size = format_bytes(path.stat().st_size)
    except OSError:
        size = "unknown size"
    return (
        f"📎 {ref.raw} ({descriptor}, {size}) — {reason} "
        f"It is available on disk at `{_agent_visible_path(path)}`. {guidance}"
    )


def _binary_reference_block(ref: ContextReference, path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return _on_disk_reference_block(
        ref, path,
        descriptor=mime,
        reason="binary file, not inlined as text.",
        guidance="Use your tools to work with it (read or convert it, extract its text, "
                 "or view/render it as needed); do not tell the user the file type is unsupported.",
    )


def _oversized_text_reference_block(ref: ContextReference, path: Path, text_tokens: int) -> str:
    return _on_disk_reference_block(
        ref, path,
        descriptor=f"text file, approximately {text_tokens} tokens",
        reason="too large to inline safely.",
        guidance="Use read_file with a narrow line range, search_files, or terminal/code tools "
                 "to inspect only the relevant parts; do not load the entire file into context.",
    )


def _file_metadata(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "unknown size"
    # A folder listing touches up to 200 entries: line-counting each file with
    # a full read_text multiplied the issue's 2x cost per file. Large files
    # report size only; small ones stay within a fixed bound.
    if size > _FOLDER_METADATA_BYTES:
        return f"{size} bytes"
    if not _is_binary_file(path):
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                text = handle.read(_FOLDER_METADATA_BYTES + 1)
            if len(text) <= _FOLDER_METADATA_BYTES:
                return f"{text.count(chr(10)) + 1} lines"
        except Exception:
            pass
    return f"{size} bytes"

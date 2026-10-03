"""One-shot, exact-head repair and canonical test execution for PR 126167.

This lives only on the auxiliary validation branch, never in the product PR.
The only published product commit is parented directly to BASE and contains
an explicit allowlist of source and regression files.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

BASE = "5dd5193b32c4fc0dbc3aa595df6c51b6f4341840"
AUX_BRANCH = "work/126167-review-fixes"
RESULT_BRANCH = "work/126167-tested-fix"
ROOT = Path.cwd()
WORK = Path(os.environ["RUNNER_TEMP"]) / "pr126167-source"
OUT = Path(os.environ["RUNNER_TEMP"]) / "pr126167-evidence"
OUT.mkdir(parents=True, exist_ok=True)
TEST = "tests/gateway/test_prompt_pin_review_lifecycle.py"
CHANGED = [
    "gateway/platforms/base.py",
    "gateway/run_agent_cache.py",
    "gateway/session_prompt_pin.py",
    "gateway/session_state.py",
    TEST,
]
report = {"base": BASE, "python": sys.version, "runs": [], "status": "started"}

NEW_TEST = r'''"""Regression scenarios reported in #126167: teardown ownership and live config.

The provider and transport are offline. Admission, timers, drain cancellation,
spooling, the slash-command writer and prompt owners are the product methods.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
import inspect

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_prompt_pin import sanitize_prompt_pin
from tests.gateway.test_internal_event_pin_wiring import (
    KEY, _capture, _drive, _human_source, _make_runner, _wake_source,
)


class OfflineAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def send(self, *args, **kwargs):
        raise AssertionError("This regression must not send a platform message")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


@pytest.mark.asyncio
@pytest.mark.parametrize("synthetic_head", [False, True], ids=["human", "synthetic"])
@pytest.mark.parametrize("queue_depth", [1, 32], ids=["one", "full"])
@pytest.mark.parametrize("expire", [False, True], ids=["unfired", "expired"])
async def test_teardown_spools_accepted_debounce_independently(
    monkeypatch, synthetic_head, queue_depth, expire,
):
    from hermes_constants import get_hermes_home

    assert Path(inspect.getfile(BasePlatformAdapter)).resolve().parents[2] == Path(__file__).resolve().parents[2]
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    adapter = OfflineAdapter(PlatformConfig(enabled=True, token="fixture"), Platform.TELEGRAM)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner.adapters, runner._profile_adapters = {Platform.TELEGRAM: adapter}, {}
    runner._primary_profile_name = "default"
    runner._sessions, runner._draining = {}, False
    runner._busy_input_mode = runner._busy_text_mode = adapter._busy_text_mode = "queue"
    adapter.gateway_runner = runner
    adapter._busy_session_handler = runner._handle_active_session_busy_message

    async def held_turn(event):
        raise AssertionError("The backed-off drain must not reach the model")

    adapter.set_message_handler(held_turn)
    source = adapter.build_source(chat_id="1001", chat_type="dm", user_id="101", message_id="201")
    head = (
        runner._synthetic_prompt_event(source, "pending-continuation")
        if synthetic_head else MessageEvent(text="pending-human", source=source, message_id="201")
    )
    key = adapter._event_session_key(head)
    adapter._active_sessions[key] = asyncio.Event()
    runner._enqueue_fifo(key, head, adapter)
    for index in range(1, queue_depth):
        runner._enqueue_fifo(
            key, MessageEvent(text=f"older-fifo-{index}", source=source), adapter,
        )
    adapter._spawn_drain_task(head, key, delay=adapter._REQUEUE_BACKOFF_MAX_SECONDS)
    human = MessageEvent(
        text="accepted-human-must-survive", message_id="202",
        source=adapter.build_source(chat_id="1001", chat_type="dm", user_id="101", message_id="202"),
    )
    spool = get_hermes_home() / "pending_messages"
    prior = set(spool.glob("*.json"))
    try:
        await adapter.handle_message(human)
        assert human._gateway_accepted
        assert key in adapter._text_debounce
        if expire:
            await asyncio.wait_for(adapter._text_debounce[key].task, timeout=5)
        assert not adapter._session_tasks[key].done()
        await adapter.cancel_background_tasks()
        payloads = [json.loads(path.read_text()) for path in set(spool.glob("*.json")) - prior]
        texts = [payload["data"]["text"] for payload in payloads]
        assert sum("accepted-human-must-survive" in text for text in texts) == 1, texts
        assert any(head.text in text for text in texts), texts
        assert all(payload["session_key"] == key for payload in payloads)
        assert not adapter._text_debounce
        # A second teardown must not duplicate already spooled events.
        saved = set(spool.glob("*.json"))
        await adapter.cancel_background_tasks()
        assert set(spool.glob("*.json")) == saved
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("change_home", [False, True], ids=["unchanged", "sethome"])
@pytest.mark.parametrize("restart", ["live", "restored", "legacy"])
@pytest.mark.parametrize("sparse", [False, True], ids=["full-source", "minimal-source"])
@pytest.mark.parametrize("internal", [False, True], ids=["continuation", "internal"])
async def test_preserved_source_observes_current_config(
    monkeypatch, tmp_path, change_home, restart, sparse, internal,
):
    assert Path(inspect.getfile(GatewayRunner)).resolve().parents[1] == Path(__file__).resolve().parents[2]
    old_id, new_id = "111111111111111111", "222222222222222222"
    config = GatewayConfig()
    config.platforms[Platform.DISCORD] = PlatformConfig(
        enabled=True, token="fixture",
        home_channel=HomeChannel(platform=Platform.DISCORD, chat_id=old_id, name="Old home"),
    )
    durable = {}
    runner = _make_runner(monkeypatch, config, durable_prompt_pin=durable)
    source = _human_source()
    before = []
    _capture(runner, before)
    await _drive(runner, ((False, source),), channel_prompt="Channel hint.")
    assert old_id in before[0]["context_prompt"]
    if change_home:
        command = MessageEvent(
            text="/sethome",
            source=dataclasses.replace(source, chat_id=new_id, chat_name="New home"),
        )
        await runner._handle_set_home_command(command)
        assert config.platforms[Platform.DISCORD].home_channel.chat_id == new_id
    if restart != "live":
        # Exercise the actual snapshot sanitizer and JSON round trip, rather
        # than transporting a Python object directly between the two runners.
        saved = sanitize_prompt_pin(durable["value"])
        assert saved is not None
        if restart == "legacy":
            saved.pop("context_source", None)
            saved.pop("shared_multi_user_session", None)
        path = tmp_path / "prompt-pin.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        durable["value"] = json.loads(path.read_text(encoding="utf-8"))
        runner = _make_runner(monkeypatch, config, durable_prompt_pin=durable)
    calls = []
    _capture(runner, calls)
    event = runner._synthetic_prompt_event(_wake_source() if sparse else source, "[heartbeat] continue")
    if internal:
        event = dataclasses.replace(event, internal=True)
    wire_source = event.source.to_dict()
    await runner._handle_message_with_agent(event, event.source, KEY, 1)
    await _drive(runner, ((False, source),), channel_prompt="Channel hint.")
    assert len(calls) == 2
    expected = new_id if change_home else old_id
    assert expected in calls[0]["context_prompt"]
    assert expected in calls[1]["context_prompt"]
    assert [call["channel_prompt"] for call in calls] == ["Channel hint."] * 2
    assert event.source.to_dict() == wire_source, "display pin must not rewrite routing source"
    assert calls[0]["source"].message_id is None
    if change_home:
        assert old_id not in calls[0]["context_prompt"]
    if restart != "legacy":
        assert calls[0]["context_prompt"] == calls[1]["context_prompt"]
        assert "Guild / #general" in calls[0]["context_prompt"]
        if not change_home:
            assert calls[0]["context_prompt"] == before[0]["context_prompt"]
'''

PIN_METHOD = '''    def _pinned_session_context_prompt(
        self, context, redact_pii: bool, session_key: Optional[str], *, preserve_pin: bool = False,
    ) -> str:
        """Preserve source identity, not stale configuration, across synthetic turns.

        The snapshot contains the display source and shared-session policy that
        produced the pinned bytes. Only those inputs are reused: current homes,
        connected platforms, tool capabilities and privacy still participate in
        the normal change key. The detached display copy never changes routing
        or authorization on the live event. Unchanged keys reuse exact bytes.
        """
        _pin_state = self._peek_session_state(session_key) if session_key else None
        _eph_pin = _pin_state.conversation.ephemeral_pin if _pin_state else None
        if preserve_pin and _eph_pin is not None:
            context = replace(
                context, source=SessionSource.from_dict(_eph_pin[3]),
                shared_multi_user_session=_eph_pin[4],
            )
        _eph_key = self._ephemeral_change_key(context, redact_pii)
        if _eph_pin is not None and _eph_pin[2] == redact_pii and _eph_pin[0] == _eph_key:
            return _eph_pin[1]
        text = build_session_context_prompt(context, redact_pii=redact_pii)
        if session_key:
            self._session_state(session_key).conversation.ephemeral_pin = (
                _eph_key, text, redact_pii, context.source.to_dict(),
                bool(context.shared_multi_user_session),
            )
        return text
'''

SANITIZER = '''def sanitize_prompt_pin(pin: Any) -> Optional[Dict[str, Any]]:
    """Validate prompt bytes and the detached source used to render them.

    Older version-1 snapshots lack source metadata. Their channel pin remains
    usable, but the context owner must render current configuration instead of
    trusting context bytes it cannot independently invalidate.
    """
    if not isinstance(pin, dict) or pin.get("version") != PROMPT_PIN_VERSION:
        return None
    context_key = pin.get("context_key")
    context_prompt = pin.get("context_prompt")
    redact_pii = pin.get("redact_pii")
    channel_prompt = pin.get("channel_prompt")
    parent_chat_id = pin.get("parent_chat_id")
    if not isinstance(context_key, str) or not context_key or not isinstance(context_prompt, str):
        return None
    if not isinstance(redact_pii, bool):
        return None
    if channel_prompt is not None and not isinstance(channel_prompt, str):
        return None
    if parent_chat_id is not None and not isinstance(parent_chat_id, str):
        return None
    cleaned = {
        "version": PROMPT_PIN_VERSION,
        "context_key": context_key,
        "context_prompt": context_prompt,
        "redact_pii": redact_pii,
        "channel_prompt": channel_prompt,
        "parent_chat_id": parent_chat_id,
    }
    if "context_source" in pin:
        from gateway.session import SessionSource

        source = pin["context_source"]
        shared = pin.get("shared_multi_user_session")
        if not isinstance(source, dict) or not isinstance(shared, bool):
            return None
        if not isinstance(source.get("platform"), str) or not isinstance(source.get("chat_id"), str):
            return None
        if "auto_thread_created" in source and not isinstance(source["auto_thread_created"], bool):
            return None
        try:
            source = SessionSource.from_dict(source).to_dict()
        except (KeyError, TypeError, ValueError):
            return None
        if any(value is not None and not isinstance(value, str)
               for name, value in source.items() if name != "auto_thread_created"):
            return None
        cleaned["context_source"] = source
        cleaned["shared_multi_user_session"] = shared
    return cleaned
'''


def run(args, *, cwd=WORK, check=True, env=None):
    print("+", " ".join(map(str, args)), flush=True)
    proc = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=600)
    print(proc.stdout, flush=True)
    if check and proc.returncode:
        raise RuntimeError(f"command failed ({proc.returncode}): {args}")
    return proc


def replace_once(path, old, new):
    file = WORK / path
    text = file.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"unexpected source shape in {path}: matches={text.count(old)}")
    file.write_text(text.replace(old, new), encoding="utf-8")


def replace_function(path, name, replacement):
    file = WORK / path
    text = file.read_text()
    nodes = [node for node in ast.walk(ast.parse(text))
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if len(nodes) != 1:
        raise RuntimeError(f"ambiguous function {path}:{name}")
    node = nodes[0]
    lines = text.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [replacement]
    file.write_text("".join(lines), encoding="utf-8")


def apply_fix():
    replace_once("gateway/platforms/base.py", '''        with contextlib.suppress(Exception):  # flush pending messages to disk before clearing
            from gateway.shutdown_flush import flush_pending_to_file
            flush_pending_to_file(self._pending_messages, reason="adapter_shutdown")
        for state in self._text_debounce_store().values():
            state.cancel_timer()
''', '''        for state in self._text_debounce_store().values():
            state.cancel_timer()
        with contextlib.suppress(Exception):  # flush pending messages to disk before clearing
            from gateway.shutdown_flush import flush_pending_to_file
            flush_pending_to_file(self._pending_messages, reason="adapter_shutdown")
        # An accepted debounce event may be incompatible with the pending head.
        # Persist it independently: shutdown cannot depend on another arrival or
        # free capacity in the live FIFO. Keep the two stores' failure scopes separate.
        with contextlib.suppress(Exception):
            from gateway.shutdown_flush import flush_pending_to_file
            flush_pending_to_file(
                {key: state.event for key, state in self._text_debounce_store().items()},
                reason="adapter_shutdown_debounce",
            )
''')
    replace_once("gateway/run_agent_cache.py", "from contextlib import nullcontext, suppress\n",
                 "from contextlib import nullcontext, suppress\nfrom dataclasses import replace\n")
    replace_once("gateway/run_agent_cache.py", '''        conversation.ephemeral_pin = (pin["context_key"], pin["context_prompt"], pin["redact_pii"])
        conversation.channel_pin = (pin["channel_prompt"], pin["parent_chat_id"])
''', '''        # Legacy snapshots can preserve the channel, but cannot prove source/config
        # independence. Warm their context once rather than replaying stale homes.
        if "context_source" in pin:
            conversation.ephemeral_pin = (
                pin["context_key"], pin["context_prompt"], pin["redact_pii"],
                pin["context_source"], pin["shared_multi_user_session"],
            )
        conversation.channel_pin = (pin["channel_prompt"], pin["parent_chat_id"])
''')
    replace_once("gateway/run_agent_cache.py", '''            "channel_prompt": channel_pin[0], "parent_chat_id": channel_pin[1],
''', '''            "channel_prompt": channel_pin[0], "parent_chat_id": channel_pin[1],
            "context_source": ephemeral_pin[3], "shared_multi_user_session": ephemeral_pin[4],
''')
    replace_function("gateway/run_agent_cache.py", "_pinned_session_context_prompt", PIN_METHOD)
    replace_function("gateway/session_prompt_pin.py", "sanitize_prompt_pin", SANITIZER)
    replace_once("gateway/session_prompt_pin.py", "            return dict(entry.prompt_pin) if entry.prompt_pin else None",
                 "            return sanitize_prompt_pin(entry.prompt_pin)")
    replace_once("gateway/session_state.py", "# pinned session-context (change_key, text, redact_pii)",
                 "# context pin: (key, text, redact_pii, display_source, shared)")
    for path in CHANGED:
        ast.parse((WORK / path).read_text(), filename=path)


def tests(label, files, *, extra=(), xml=False):
    env = os.environ.copy()
    env.pop("__HERMES_ACTIVATED", None)
    env["HERMES_PYTHON"] = sys.executable
    env["HERMES_TEST_FILE_RETRIES"] = "0"
    env["HERMES_TEST_WORKERS"] = "2"
    args = ["bash", "scripts/run_tests.sh", *files, "-q", *extra]
    xml_path = OUT / (label + ".xml")
    if xml:
        args.append(f"--junitxml={xml_path}")
    start = time.monotonic()
    proc = run(args, check=False, env=env)
    (OUT / (label + ".log")).write_text(proc.stdout)
    receipt = {"label": label, "exit": proc.returncode, "seconds": round(time.monotonic()-start, 3),
               "files": files, "extra": list(extra), "log_tail": proc.stdout[-9000:]}
    if xml_path.exists():
        cases = ET.parse(xml_path).findall(".//testcase")
        receipt.update(
            total=len(cases),
            failures=[case.get("name") for case in cases if case.find("failure") is not None],
            errors=[case.get("name") for case in cases if case.find("error") is not None],
            skipped=[case.get("name") for case in cases if case.find("skipped") is not None],
        )
    report["runs"].append(receipt)
    return receipt


def main():
    run(["git", "fetch", "origin", BASE], cwd=ROOT)
    run(["git", "worktree", "add", "--detach", str(WORK), BASE], cwd=ROOT)
    run([sys.executable, "-c", "import pytest; print(pytest.__version__)"], cwd=WORK)
    (WORK / TEST).write_text(NEW_TEST, encoding="utf-8")
    red = tests("red_review_counterexamples", [TEST], xml=True)
    if red.get("errors") or red.get("total", 0) != 32 or not red.get("failures"):
        raise RuntimeError("baseline did not produce the expected behavioral counterexamples")
    apply_fix()
    green = tests("green_review_counterexamples", [TEST], xml=True)
    if green["exit"] or green.get("failures") or green.get("errors") or green.get("total") != 32:
        raise RuntimeError("review regression matrix is not green")
    focused = [
        "tests/gateway/test_active_session_text_merge.py",
        "tests/gateway/test_goal_resume_restart.py",
        "tests/gateway/test_internal_event_pin_wiring.py",
        "tests/gateway/test_queue_consumption.py",
        "tests/gateway/test_goal_continuation_drain.py",
        "tests/gateway/test_heartbeat_poller.py",
        "tests/gateway/test_prompt_tail_freeze.py",
        "tests/gateway/test_session.py",
    ]
    receipt = tests("existing_focused_suite", focused)
    if receipt["exit"]:
        raise RuntimeError("existing focused suite is not green")
    # Ten bounded post-fix passes, each with a stated behavioral target. These
    # are single-agent checks, not claims of ten independent reviewers.
    passes = [
        ("01_expired_debounce", [TEST], ("-k", "teardown and expired")),
        ("02_unfired_debounce", [TEST], ("-k", "teardown and unfired")),
        ("03_full_live_fifo", [TEST], ("-k", "teardown and full")),
        ("04_sethome", [TEST], ("-k", "preserved_source and sethome")),
        ("05_unchanged_source_bytes", [TEST], ("-k", "preserved_source and unchanged")),
        ("06_restart_and_legacy", [TEST], ("-k", "preserved_source and (restored or legacy)")),
        ("07_minimal_source", [TEST], ("-k", "preserved_source and minimal-source")),
        ("08_privacy_and_effective_prompt", ["tests/gateway/test_internal_event_pin_wiring.py"], ()),
        ("09_source_provenance", ["tests/gateway/test_goal_resume_restart.py", "tests/gateway/test_goal_continuation_drain.py"], ()),
        ("10_sender_capacity_fifo", ["tests/gateway/test_active_session_text_merge.py", "tests/gateway/test_queue_consumption.py"], ()),
    ]
    for label, selection, extra in passes:
        receipt = tests(label, selection, extra=extra)
        if receipt["exit"]:
            raise RuntimeError(f"post-fix pass {label} failed")
    run(["git", "add", "--", *CHANGED])
    changed = run(["git", "diff", "--cached", "--name-only"]).stdout.splitlines()
    if sorted(changed) != sorted(CHANGED):
        raise RuntimeError(f"unexpected publication paths: {changed}")
    lint = run([sys.executable, "-m", "ruff", "check", *CHANGED], check=False)
    report["lint"] = {"exit": lint.returncode, "output": lint.stdout}
    if lint.returncode:
        raise RuntimeError("changed-file lint is not green")
    run(["git", "diff", "--cached", "--check"])
    diff = run(["git", "diff", "--cached", "--stat"]).stdout
    report["diff_stat"] = diff
    report["sha256"] = {path: hashlib.sha256((WORK/path).read_bytes()).hexdigest() for path in CHANGED}
    tree = run(["git", "write-tree"]).stdout.strip()
    identity = os.environ.copy()
    identity.update(GIT_AUTHOR_NAME="JoaoMarcos44", GIT_AUTHOR_EMAIL="joaomarcosdias444@gmail.com",
                    GIT_COMMITTER_NAME="JoaoMarcos44", GIT_COMMITTER_EMAIL="joaomarcosdias444@gmail.com")
    commit = run(["git", "commit-tree", tree, "-p", BASE, "-m",
                  "fix(gateway): preserve debounce and refresh pinned config"], env=identity).stdout.strip()
    report.update(commit=commit, tree=tree, status="validated")
    # Never update the PR here. Publish the tested object only; the connected
    # caller will re-read the PR and fast-forward its exact branch separately.
    run(["git", "push", "origin", f"{commit}:refs/heads/{RESULT_BRANCH}"])


try:
    main()
except Exception as exc:
    report.update(status="failed", error=str(exc), traceback=traceback.format_exc())
    raise
finally:
    # Publish receipts on the auxiliary branch even if a test fails. This has
    # no effect on the PR head and cannot disguise failure as a product fix.
    (ROOT / "review126167-results.json").write_text(json.dumps(report, indent=2) + "\n")
    run(["git", "add", "review126167-results.json"], cwd=ROOT)
    run(["git", "commit", "-m", "chore: record PR 126167 test evidence"], cwd=ROOT)
    run(["git", "push", "origin", f"HEAD:refs/heads/{AUX_BRANCH}"], cwd=ROOT)

"""A multiplexed profile never adopts another profile's live MCP connection when the inputs that
connection was opened with resolve differently in its own scope, even when the two ``mcp_servers``
configs are textually identical: the stdio child's external secret-source env, the HTTP
``identity_header`` (static or ``value_from: profile``) and the live-endpoint Bearer. Both E2E
cases drive the real ``_discover_gateway_mcp_tools(GatewayConfig(multiplex_profiles=True))``
against two profile homes and real MCP servers (a stdio child and a streamable-HTTP server that
records what it receives on the wire). Regression for #122665."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_yaml as yaml

pytest.importorskip("mcp")

_MODEL = {"default": "test-model", "provider": "custom", "base_url": "http://127.0.0.1:9/v1"}

# Minimal stdio MCP server: reports the identity its child process actually runs with.
_STDIO_SERVER = '''
import json
import os
from mcp.server import MCPServer

server = MCPServer("whoami")

@server.tool()
def whoami() -> str:
    return json.dumps({"pid": os.getpid(),
                       "token": os.environ.get("GH_TOKEN", ""),
                       "cwd": os.getcwd()})

server.run(transport="stdio")
'''

# Minimal streamable-HTTP MCP server: records the identity headers of every tools/call it serves.
_HTTP_SERVER = '''
import asyncio
import json
import socket
import sys
import uvicorn
from mcp.server import MCPServer

log_path, port_path = sys.argv[1], sys.argv[2]
server = MCPServer("team")

@server.tool()
def save_note(text: str) -> str:
    return "saved"

def record(inner):
    async def app(scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await inner(scope, receive, send)
        body = b""
        while True:
            event = await receive()
            body += event.get("body", b"")
            if not event.get("more_body"):
                break
        try:
            parsed = json.loads(body or b"null")
        except ValueError:
            parsed = {}
        if parsed.get("method") == "tools/call":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "text": (parsed.get("params") or {}).get("arguments", {}).get("text"),
                    "profile": headers.get("x-hermes-profile"),
                    "user": headers.get("x-user"),
                    "authorization": headers.get("authorization"),
                }) + "\\n")
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await inner(scope, replay, send)
    return app

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 0))
uv = uvicorn.Server(uvicorn.Config(record(server.streamable_http_app()), log_level="warning"))

async def main():
    task = asyncio.create_task(uv.serve(sockets=[sock]))
    while not uv.started:
        await asyncio.sleep(0.01)
    with open(port_path, "w", encoding="utf-8") as fh:
        fh.write(str(sock.getsockname()[1]))
    await task

asyncio.run(main())
'''


@pytest.fixture
def two_profile_homes(tmp_path, monkeypatch):
    """default + worker profile homes under a temp HOME; live MCP connections shut down after."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    default_home = tmp_path / ".hermes"
    worker_home = default_home / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("NO_PROXY", "*")
    yield {"default": default_home, "worker": worker_home}
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    shutdown_mcp_servers()


def _discover_and_call(homes: dict, tool: str, args_for) -> dict:
    """One real multiplexed discovery pass, then a real dispatch per profile scope."""
    import gateway.run as gateway_run
    from gateway.config import GatewayConfig
    from tools.registry import registry

    asyncio.run(gateway_run._discover_gateway_mcp_tools(GatewayConfig(multiplex_profiles=True)))
    results = {}
    for name, home in homes.items():
        with gateway_run._profile_runtime_scope(home):
            results[name] = json.loads(registry.dispatch(tool, args_for(name))).get("result")
    return results


def _write_config(home: Path, mcp_server: dict, extra: dict | None = None) -> None:
    cfg = {"model": _MODEL, "mcp_servers": {"srv": mcp_server}}
    cfg.update(extra or {})
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))


def _http_server(tmp_path):
    """Context manager: real streamable-HTTP fixture server -> (url, inbound log path)."""
    import contextlib

    @contextlib.contextmanager
    def _running():
        pytest.importorskip("uvicorn")
        log, port_file = tmp_path / "calls.log", tmp_path / "port"
        script = tmp_path / "team_server.py"
        script.write_text(_HTTP_SERVER)
        proc = subprocess.Popen([sys.executable, str(script), str(log), str(port_file)])
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not (port_file.exists() and port_file.read_text().strip()):
                time.sleep(0.05)
            port = port_file.read_text().strip()
            assert port, "fixture HTTP server never reported its port"
            yield f"http://127.0.0.1:{port}/mcp", log
        finally:
            proc.terminate()
            proc.wait(10)

    return _running()


def _records(log: Path) -> list:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_secret_source_env_is_per_profile_and_never_shared_across_profiles(
    two_profile_homes, tmp_path, monkeypatch
):
    """Same static stdio config, two profiles whose secret source resolves GH_TOKEN differently:
    each profile's tool call must run in a child carrying ITS OWN token. Portable seam: the two
    functions ``mcp_tool_config._build_safe_env`` resolves secret-source values through."""
    from hermes_cli.profiles import get_active_profile_name

    server = tmp_path / "whoami_server.py"
    server.write_text(_STDIO_SERVER)
    monkeypatch.setattr("hermes_cli.env_loader.secret_source_names", lambda: ("GH_TOKEN",))
    monkeypatch.setattr(
        "agent.secret_scope.get_secret",
        lambda key: {"GH_TOKEN": f"fake-token-{get_active_profile_name()}"}.get(key),
    )
    for home in two_profile_homes.values():
        _write_config(home, {"command": sys.executable, "args": [str(server)]})

    results = _discover_and_call(two_profile_homes, "mcp__srv__whoami", lambda _name: {})
    payloads = {name: json.loads(raw) for name, raw in results.items()}

    assert payloads["default"]["token"] == "fake-token-default"
    assert payloads["worker"]["token"] == "fake-token-worker"


@pytest.mark.platforms("posix")  # the ``command`` secret source is POSIX-only by contract
def test_command_secret_source_values_are_per_profile(two_profile_homes, tmp_path):
    """The reporter's exact mechanism: a real ``secrets.command`` helper per profile home."""
    server = tmp_path / "whoami_server.py"
    server.write_text(_STDIO_SERVER)
    for name, home in two_profile_homes.items():
        (home / "secrets.env").write_text(f"GH_TOKEN=fake-token-{name}\n")
        _write_config(
            home,
            {"command": sys.executable, "args": [str(server)]},
            extra={"secrets": {"command": {"enabled": True,
                                           "command": f"cat {home / 'secrets.env'}"}}},
        )

    results = _discover_and_call(two_profile_homes, "mcp__srv__whoami", lambda _name: {})
    payloads = {name: json.loads(raw) for name, raw in results.items()}

    assert payloads["default"]["token"] == "fake-token-default"
    assert payloads["worker"]["token"] == "fake-token-worker"


def test_identity_header_value_from_profile_never_serves_another_profiles_connection(
    two_profile_homes, tmp_path
):
    """With ``identity_header.value_from: profile`` the server must see each profile's own name."""
    with _http_server(tmp_path) as (url, log):
        for home in two_profile_homes.values():
            _write_config(home, {"url": url,
                                 "identity_header": {"name": "X-Hermes-Profile", "value_from": "profile"}})
        _discover_and_call(two_profile_homes, "mcp__srv__save_note",
                           lambda name: {"text": f"hi-{name}"})

        by_text = {rec["text"]: rec for rec in _records(log) if rec.get("text")}
        assert by_text["hi-default"]["profile"] == "default"
        assert by_text["hi-worker"]["profile"] == "worker"


def test_differing_static_identity_header_values_get_their_own_connections(
    two_profile_homes, tmp_path
):
    """Static identities that merely differ between profiles are two identities too."""
    identities = {"default": "alice", "worker": "bob"}
    with _http_server(tmp_path) as (url, log):
        for name, home in two_profile_homes.items():
            _write_config(home, {"url": url,
                                 "identity_header": {"name": "X-User", "value": identities[name]}})
        _discover_and_call(two_profile_homes, "mcp__srv__save_note",
                           lambda name: {"text": f"hi-{name}"})

        by_text = {rec["text"]: rec for rec in _records(log) if rec.get("text")}
        assert by_text["hi-default"]["user"] == "alice"
        assert by_text["hi-worker"]["user"] == "bob"


def test_live_endpoint_credentials_are_re_resolved_before_cross_profile_adoption(
    two_profile_homes, tmp_path, monkeypatch
):
    """A live endpoint's URL/Bearer is a connect input like any other: when the value current at
    the adopter's pass differs from what the connection was opened with, the adopter must open its
    own connection instead of riding the owner's endpoint credentials."""
    with _http_server(tmp_path) as (url, log):
        seen: list = []

        def fake_live_endpoint(server_name):
            seen.append(server_name)
            token = "token-owner" if len(seen) == 1 else "token-adopter"
            return url, {"Authorization": f"Bearer {token}"}

        monkeypatch.setattr("tools.mcp_tool_transport._live_endpoint", fake_live_endpoint)
        for home in two_profile_homes.values():
            _write_config(home, {"url": url})

        # Phase 1: only the default profile discovers (and connects with the owner token).
        monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve",
                            lambda multiplex: [("default", two_profile_homes["default"])])
        _discover_and_call({"default": two_profile_homes["default"]}, "mcp__srv__save_note",
                           lambda _name: {"text": "hi-default"})
        # Phase 2: the endpoint's credentials have moved on; the worker must resolve its own.
        monkeypatch.setattr("hermes_cli.profiles.profiles_to_serve",
                            lambda multiplex: list(two_profile_homes.items()))
        _discover_and_call({"worker": two_profile_homes["worker"]}, "mcp__srv__save_note",
                           lambda _name: {"text": "hi-worker"})

        by_text = {rec["text"]: rec for rec in _records(log) if rec.get("text")}
        assert by_text["hi-default"]["authorization"] == "Bearer token-owner"
        assert by_text["hi-worker"]["authorization"] == "Bearer token-adopter"


def test_equal_resolved_inputs_still_share_one_connection(two_profile_homes, tmp_path):
    """Sharing survives: profiles whose effective inputs resolve identically reuse the ONE live
    connection (same stdio child) instead of opening one each."""
    server = tmp_path / "whoami_server.py"
    server.write_text(_STDIO_SERVER)
    for home in two_profile_homes.values():
        _write_config(home, {"command": sys.executable, "args": [str(server)]})

    results = _discover_and_call(two_profile_homes, "mcp__srv__whoami", lambda _name: {})
    payloads = {name: json.loads(raw) for name, raw in results.items()}

    assert payloads["default"]["pid"] == payloads["worker"]["pid"]
    assert payloads["default"]["pid"] > 0

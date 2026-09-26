"""What an MCP connection is opened WITH, for cross-profile connection sharing.

Under a multiplexed gateway one live connection may serve several profiles, but only when every
value the transport resolved to open it resolves to the same value in the adopter's own profile
scope: a stdio child's env (external secret sources resolve per profile) and default cwd, an HTTP
connection's headers after ``identity_header`` (``value_from: profile`` names the profile) and its
live-endpoint URL/Bearer. Static config equality cannot prove any of that — the connecting task
records a keyed digest of its effective inputs, and a cross-profile adopter recomputes it in its
own scope (``tools.mcp_tool_registration._same_server_route``); a mismatch — or a resolution
failure — refuses the share and the adopter opens its own connection.

Parity by construction: both the digest and the transports consume the SAME
``effective_*_inputs`` functions, so the recorded digest is definitionally what the connection was
opened with. The only steps after resolution are the OSV malware preflight and the cached-npx
swap (``tools.mcp_tool._preflight_stdio_command``) — local execution optimizations (network
check, user-level npm cache) that resolve no per-profile value and are deliberately outside the
projection. Only the HMAC is kept; resolved secrets are never retained or logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

logger = logging.getLogger("tools.mcp_tool")

# Process-local key: a leaked digest must not allow offline guessing of low-entropy secrets.
_MAC_KEY = os.urandom(32)


def _mac(projection) -> str:
    """Keyed digest of one canonical projection of effective connect inputs."""
    canonical = json.dumps(projection, sort_keys=True, default=str).encode("utf-8")
    return hmac.new(_MAC_KEY, canonical, hashlib.sha256).hexdigest()


def effective_stdio_inputs(server_name: str, config: dict) -> tuple:
    """``(command, args, env, cwd)`` exactly as ``_run_stdio`` spawns a stdio child: the filtered
    env (safe keys plus the active profile's external secret-source values), the command resolved
    against THAT env (``_resolve_stdio_command`` can pick a per-profile managed Node), and the
    explicit or session default cwd. A stdio child inherits this process's cwd when none is
    configured. Hosted sessions (ACP, gateway) pin a logical cwd via ``agent.runtime_cwd``;
    without it the child resolves relative paths against the daemon's launch dir, not the session
    workspace. Explicit config always wins; an existing session/TERMINAL_CWD anchor becomes the
    default; else native (None)."""
    from agent.runtime_cwd import resolve_context_cwd
    from tools.mcp_tool_config import _build_safe_env, _resolve_stdio_command

    command = config.get("command")
    if not command:
        raise ValueError(f"MCP server '{server_name}' has no 'command' in config")
    command, env = _resolve_stdio_command(command, _build_safe_env(config.get("env")))
    cwd = config.get("cwd")
    if cwd is None:
        cwd = resolve_context_cwd() or None
    return command, config.get("args", []), env, cwd


def effective_http_inputs(server_name: str, config: dict) -> tuple:
    """``(url, headers, configured_header_names)`` exactly as ``_run_http`` connects: the live
    endpoint (if the app runtime provides one) replaces the URL and merges its headers BEFORE the
    redirect-boundary name capture and the identity header, and the handshake protocol-version
    header is seeded when the user did not set one (a process-wide constant, no identity)."""
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_errors import _apply_identity_header
    from tools.mcp_tool_transport import _live_endpoint

    url = config["url"]
    headers = dict(config.get("headers") or {})
    live = _live_endpoint(server_name)
    if live is not None:
        url, live_headers = live
        headers.update(live_headers)
    # Agent Plugins v1 strict_redirect_headers: configured headers MUST NOT follow a cross-origin
    # redirect — capture their names BEFORE client-generated headers are merged in.
    configured_header_names = {key.lower() for key in headers}
    headers = _apply_identity_header(server_name, config, headers)  # explicit same-name headers win
    # Seed MCP-Protocol-Version (user override wins) from the HANDSHAKE version, not the latest: a
    # 2026-07-28 header routes the handshake-era ``initialize()`` onto the envelope ladder, which rejects it.
    if not any(key.lower() == "mcp-protocol-version" for key in headers):
        headers["mcp-protocol-version"] = _core.LATEST_HANDSHAKE_VERSION
    return url, headers, configured_header_names


def stdio_identity(command, args, env, cwd) -> str:
    return _mac(("stdio", command, args, env, cwd))


def http_identity(url: str, headers: dict) -> str:
    return _mac(("http", url, headers))


def resolved_connection_identity(server_name: str, config: dict) -> str | None:
    """Digest of the connection *config* would open, resolved in the CURRENT profile scope; None
    when resolution fails — a connection whose effective inputs cannot be proven equal is never
    shared across profiles (fail closed), and one failed name never aborts the discovery pass."""
    try:
        if "url" in config:
            url, headers, _configured = effective_http_inputs(server_name, config)
            return http_identity(url, headers)
        command, args, env, cwd = effective_stdio_inputs(server_name, config)
        return stdio_identity(command, args, env, cwd)
    except Exception as exc:  # resolution failure is a refusal, never a discovery abort
        logger.debug("MCP server '%s': effective connection identity unresolvable (%s) — "
                     "cross-profile sharing refused", server_name, type(exc).__name__)
        return None

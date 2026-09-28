"""HTTP server that forwards OpenAI-compatible requests to a configured upstream.

A credential-attaching forwarder: request/response bodies are never mediated, logged, or rewritten.
The one shim: after a *clean* upstream EOF, a ``text/event-stream`` response that carried a terminal
``finish_reason`` or ``lastOne: true`` but omitted ``data: [DONE]`` gets a single ``[DONE]`` frame.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from ipaddress import ip_address
from typing import Optional
from urllib.parse import urlsplit

try:
    import aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.proxy.sse_done import DONE_SSE_FRAME, SseDoneTracker, content_type_is_sse

logger = logging.getLogger(__name__)

# Stripped when forwarding upstream: ``host``/``content-length`` are recomputed by aiohttp,
# ``authorization`` is replaced with our bearer; everything else passes through.
_HOP_BY_HOP_HEADERS = frozenset({
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "authorization",
})
# aiohttp recomputes Content-Encoding/Content-Length on stream — let it.
_RESPONSE_DROP_HEADERS = _HOP_BY_HOP_HEADERS | {"content-encoding", "content-length"}

DEFAULT_PORT = 8645
DEFAULT_HOST = "127.0.0.1"
# Mirrors api_server's MAX_REQUEST_BYTES (10 MB); client_max_size bounds every read path,
# including chunked bodies.
MAX_REQUEST_BYTES = 10_000_000


def _require_aiohttp() -> None:
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for `hermes proxy`. Run `hermes setup` to install it.")


def _json_error(status: int, message: str, code: str = "proxy_error") -> "web.Response":
    """OpenAI-style error JSON response."""
    body = {"error": {"message": message, "type": code, "code": code}}
    return web.json_response(body, status=status)


def _canonical_hostname(hostname: str) -> str:
    """Normalize a Host/Origin hostname for exact security-boundary comparison."""
    value = hostname.rstrip(".").lower()
    try:
        return ip_address(value).compressed
    except ValueError:
        return value


def _parse_authority(authority: str) -> Optional[tuple[str, Optional[int]]]:
    """Return canonical (hostname, port) for an HTTP authority, or None if malformed."""
    if not authority or any(char in authority for char in "@/\\?# \t\r\n"):
        return None
    try:
        parsed = urlsplit("//" + authority)
        port = parsed.port
    except ValueError:
        return None
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        return None
    return _canonical_hostname(parsed.hostname), port


def _normalized_bind_host(bound_host: str) -> str:
    value = bound_host.strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return _canonical_hostname(value)


def _is_wildcard_bind(bound_host: str) -> bool:
    normalized = _normalized_bind_host(bound_host)
    if not normalized:
        return True
    try:
        return ip_address(normalized).is_unspecified
    except ValueError:
        return False


def _origin_matches_authority(origin: str, authority: str) -> bool:
    """Whether a browser Origin is the same plain-HTTP origin as the request Host authority."""
    target = _parse_authority(authority)
    if target is None or not origin or any(char in origin for char in " \t\r\n"):
        return False
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return (
        _canonical_hostname(parsed.hostname) == target[0]
        and (origin_port or 80) == (target[1] or 80)
    )


_LOOPBACK_REQUEST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _request_boundary_error(host_header: str, origin: Optional[str], bound_host: str) -> Optional[str]:
    """Return a refusal code for requests that must not reach the credential-attaching proxy."""
    authority = _parse_authority(host_header)
    if authority is None:
        return "host_not_allowed"

    hostname, _ = authority
    wildcard = _is_wildcard_bind(bound_host)
    if not wildcard:
        allowed_hosts = _LOOPBACK_REQUEST_HOSTS | {_normalized_bind_host(bound_host)}
        if hostname not in allowed_hosts:
            return "host_not_allowed"

    if origin is not None:
        if wildcard or not _origin_matches_authority(origin.strip(), host_header):
            return "origin_not_allowed"

    return None


def _filter_headers(headers, drop: frozenset = _HOP_BY_HOP_HEADERS) -> dict:
    """Strip hop-by-hop (+ auth) headers; ``drop`` widens the set for upstream responses."""
    return {key: value for key, value in headers.items() if key.lower() not in drop}


async def _open_upstream(request: "web.Request", rel_path: str, body: bytes, cred: UpstreamCredential):
    """Send the request upstream with ``cred``; returns ``(session, response)`` or
    ``(error_response, None)``."""
    upstream_url = f"{cred.base_url.rstrip('/')}{rel_path}"
    if request.query_string:  # preserved verbatim
        upstream_url = f"{upstream_url}?{request.query_string}"
    fwd_headers = _filter_headers(request.headers)
    fwd_headers["Authorization"] = f"{cred.token_type} {cred.bearer}"
    logger.debug("proxy: forwarding %s %s -> %s (body=%d bytes)", request.method, rel_path, upstream_url, len(body))
    try:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300))
    except Exception as exc:  # pragma: no cover - aiohttp setup issue
        return _json_error(500, f"proxy session init failed: {exc}"), None
    try:
        upstream_resp = await session.request(
            request.method, upstream_url, data=body if body else None, headers=fwd_headers, allow_redirects=False
        )
    except RuntimeError as exc:
        await session.close()
        return _json_error(500, str(exc)), None
    except aiohttp.ClientError as exc:
        await session.close()
        logger.warning("proxy: upstream connection failed: %s", exc)
        return _json_error(502, f"upstream connection failed: {exc}", code="upstream_unreachable"), None
    except asyncio.TimeoutError:
        await session.close()
        return _json_error(504, "upstream request timed out", code="upstream_timeout"), None
    except Exception:
        await session.close()
        raise
    return session, upstream_resp


async def _stream_back(request: "web.Request", session, upstream_resp) -> "web.StreamResponse":
    """Relay status + filtered headers, then the body chunk-by-chunk, appending a missing SSE
    ``[DONE]`` only after a clean EOF."""
    resp = web.StreamResponse(
        status=upstream_resp.status, headers=_filter_headers(upstream_resp.headers, _RESPONSE_DROP_HEADERS)
    )
    await resp.prepare(request)
    done_tracker: Optional[SseDoneTracker] = None
    if content_type_is_sse(upstream_resp.headers):
        done_tracker = SseDoneTracker()
    try:
        async for chunk in upstream_resp.content.iter_any():
            if chunk:
                if done_tracker is not None:
                    done_tracker.feed(chunk)
                await resp.write(chunk)
        if done_tracker is not None and done_tracker.should_append_done():
            try:
                await resp.write(DONE_SSE_FRAME)
            except Exception as exc:  # client hung up at EOF — harmless
                logger.debug("proxy: DONE append skipped: %s", exc)
    except (aiohttp.ClientError, asyncio.CancelledError, OSError) as exc:
        if done_tracker is not None:
            done_tracker.mark_interrupted()
        logger.warning("proxy: streaming interrupted: %s", exc)
    finally:
        upstream_resp.release()
        await session.close()
    await resp.write_eof()
    return resp


def create_app(adapter: UpstreamAdapter, bound_host: str = DEFAULT_HOST) -> "web.Application":
    """Build the aiohttp application bound to a specific upstream adapter.

    Every adapter method is synchronous and blocking (the Nous adapter takes the 15s cross-process
    ``_auth_store_lock()`` and may POST a token refresh; xAI rotates its key pool under a lock),
    so all three are run via ``asyncio.to_thread`` — a contended lock or refresh must never freeze
    the single loop and every other in-flight streaming completion.
    """
    _require_aiohttp()

    @web.middleware
    async def enforce_local_request_boundary(request: "web.Request", handler):
        refusal = _request_boundary_error(
            request.headers.get("Host", ""),
            request.headers.get("Origin"),
            bound_host,
        )
        if refusal:
            return _json_error(
                403,
                "Request refused by the local proxy Host/Origin boundary.",
                code=refusal,
            )
        return await handler(request)

    app = web.Application(
        client_max_size=MAX_REQUEST_BYTES,
        middlewares=[enforce_local_request_boundary],
    )
    # AppKey: forward-compat with aiohttp versions that strip bare-string keys.
    app[web.AppKey("adapter", UpstreamAdapter)] = adapter

    async def handle_health(request: "web.Request") -> "web.Response":
        authenticated = await asyncio.to_thread(adapter.is_authenticated)
        return web.json_response({"status": "ok", "upstream": adapter.display_name, "authenticated": authenticated})

    async def handle_proxy(request: "web.Request") -> "web.StreamResponse":
        rel_path = "/" + request.match_info.get("tail", "").lstrip("/")
        if rel_path not in adapter.allowed_paths:
            allowed = ", ".join(sorted(adapter.allowed_paths))
            return _json_error(
                404, f"Path /v1{rel_path} is not forwarded by this proxy. Allowed: {allowed}", code="path_not_allowed"
            )
        try:
            cred = await asyncio.to_thread(adapter.get_credential)
        except Exception as exc:
            logger.warning("proxy: credential resolution failed: %s", exc)
            return _json_error(401, str(exc), code="upstream_auth_failed")
        # Body read into memory once (chat/embeddings payloads are small); switch to streaming
        # if large multipart uploads ever need forwarding.
        body = await request.read()
        session, upstream_resp = await _open_upstream(request, rel_path, body, cred)
        if upstream_resp is None:
            return session
        if upstream_resp.status in {401, 429}:
            # One-shot retry with a refreshed/rotated credential (Nous: unconditional refresh
            # POST under the auth lock; xAI: pool rotation).
            try:
                retry_cred = await asyncio.to_thread(
                    adapter.get_retry_credential, failed_credential=cred, status_code=upstream_resp.status
                )
            except Exception as exc:
                logger.warning("proxy: retry credential resolution failed: %s", exc)
                retry_cred = None
            if retry_cred is not None:
                upstream_resp.release()
                await session.close()
                session, upstream_resp = await _open_upstream(request, rel_path, body, retry_cred)
                if upstream_resp is None:
                    return session
        return await _stream_back(request, session, upstream_resp)

    app.router.add_get("/health", handle_health)  # never goes upstream
    app.router.add_route("*", "/v1/{tail:.*}", handle_proxy)  # forwards if the path is allowed
    return app


async def run_server(
    adapter: UpstreamAdapter,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Run the proxy in the current event loop until shutdown_event is set."""
    _require_aiohttp()
    app = create_app(adapter, bound_host=host)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("proxy: listening on http://%s:%d/v1 -> %s", host, port, adapter.display_name)
    stop_event = shutdown_event or asyncio.Event()
    if shutdown_event is None:  # we own the loop's lifetime → wire signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)  # windows-footgun: ok
            except NotImplementedError:
                pass  # Windows / restricted envs — Ctrl+C still raises KeyboardInterrupt
    try:
        await stop_event.wait()
    finally:
        logger.info("proxy: shutting down")
        await runner.cleanup()


__all__ = ["create_app", "run_server", "DEFAULT_HOST", "DEFAULT_PORT", "AIOHTTP_AVAILABLE"]

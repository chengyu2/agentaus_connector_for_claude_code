"""Requests for Claude models, forwarded to api.anthropic.com unchanged."""

from __future__ import annotations

import hmac
import logging
import time
from typing import AsyncIterator

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from .bridge_responses import _error_response
from .config import settings
from .request_log import rlog
from .translate import AnthropicStreamBuilder
from .upstream_http import (
    _PASSTHROUGH_RETRYABLE_STATUS,
    _describe,
    _is_retryable_exception,
    _sleep_before_retry,
)


# Headers we must not copy verbatim when proxying (hop-by-hop or recomputed).
_STRIP_REQUEST_HEADERS = {
    "host", "content-length", "connection", "transfer-encoding", "accept-encoding",
}
# The bridge's own auth header. It exists so a client can hold both a bridge secret and a
# real Anthropic credential at once; it is for this bridge only and must never be forwarded.
BRIDGE_TOKEN_HEADER = "x-agentaus-bridge-token"
_STRIP_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
}


def _same_secret(supplied: str, expected: str) -> bool:
    """Constant-time comparison of two tokens."""
    return hmac.compare_digest((supplied or "").encode(), (expected or "").encode())


def _header_carries_bridge_token(name: str, value: str) -> bool:
    """Whether this header's value IS the bridge token (a credential must not be dropped
    merely for living in the same header name as one)."""
    token = settings.bridge_token
    if not token:
        return False
    lowered = (name or "").lower()
    value = value or ""
    if lowered == "x-api-key":
        return _same_secret(value, token)
    if lowered == "authorization":
        if _same_secret(value, token):
            return True
        if value.lower().startswith("bearer "):
            return _same_secret(value[7:].strip(), token)
    return False


def _anthropic_request_headers(request: Request) -> dict:
    """The headers a request to Anthropic should carry, sanitised of the bridge.

    Everything the caller sent goes through except hop-by-hop headers and the bridge's
    own credential, in whichever header it actually arrived in - a legacy client that
    authenticated with `x-api-key: <bridge token>` would otherwise hand that secret to
    Anthropic, and a client that authenticated with the dedicated header would leak it
    the same way. A separate, different value in the other auth header - the caller's
    real Anthropic key or OAuth token - is kept.
    """
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _STRIP_REQUEST_HEADERS and key.lower() != BRIDGE_TOKEN_HEADER
    }
    for name, value in list(headers.items()):
        if _header_carries_bridge_token(name, value):
            del headers[name]
    return headers


_WARNED_BRIDGE_TOKEN_ONLY_CREDENTIAL = False


def _warn_if_bridge_token_was_the_only_credential(request: Request, headers: dict) -> None:
    """Say why Anthropic is about to answer 401, once per process.

    Before the dedicated header existed, running BRIDGE_TOKEN with Claude passthrough
    only worked by setting BRIDGE_TOKEN to the caller's own Anthropic key, which was then
    forwarded unchanged. That header is now stripped as the bridge token, so such a setup
    reaches Anthropic with no credential at all and nothing else in the log says why.
    """
    global _WARNED_BRIDGE_TOKEN_ONLY_CREDENTIAL
    if _WARNED_BRIDGE_TOKEN_ONLY_CREDENTIAL:
        return
    auth_names = {"x-api-key", "authorization"}
    if any(key.lower() in auth_names for key in headers):
        return  # a real credential survived
    if not any(_header_carries_bridge_token(key, value)
               for key, value in request.headers.items() if key.lower() in auth_names):
        return  # the caller sent no credential to strip; Anthropic's 401 is self-explanatory
    _WARNED_BRIDGE_TOKEN_ONLY_CREDENTIAL = True
    rlog(
        logging.WARNING,
        "Claude request forwarded to Anthropic with no credential: the only x-api-key/"
        "Authorization the client sent equals BRIDGE_TOKEN, and the bridge token is never "
        "passed upstream. If BRIDGE_TOKEN is your Anthropic key, set BRIDGE_TOKEN to a "
        "separate secret, send it as the %s header (Claude Code: ANTHROPIC_CUSTOM_HEADERS), "
        "and keep your real key in x-api-key.",
        BRIDGE_TOKEN_HEADER,
    )


# --------------------------------------------------------------------------------------
# Anthropic passthrough
# --------------------------------------------------------------------------------------

_UPLOAD_SLICE = 64 * 1024


async def _upload_slices(raw: bytes) -> AsyncIterator[bytes]:
    """Yield a request body in slices so the write timeout applies to each slice.

    httpcore writes one `bytes` body as a single chunk inside one write timeout, so a
    multi-megabyte Claude request (screenshots make 3-20 MB common) had to finish
    uploading within BRIDGE_CONNECT_TIMEOUT or fail with WriteTimeout and start over.
    Per slice, a dead connection still times out but a slow uplink does not.
    """
    view = memoryview(raw)
    for start in range(0, len(view), _UPLOAD_SLICE):
        yield bytes(view[start:start + _UPLOAD_SLICE])


async def _passthrough(request: Request, raw: bytes) -> Response:
    """Forward a request to api.anthropic.com unchanged.

    Headers go through verbatim: `anthropic-beta` carries the OAuth capability that a
    claude.ai subscription login needs, and error bodies must stay unmodified or
    Claude Code's capability-rejection retry logic stops matching on them. "Verbatim"
    except the bridge's own credential - `_anthropic_request_headers` strips the
    dedicated bridge header and any auth header whose value IS the bridge token, so the
    secret never reaches Anthropic while the caller's real key or OAuth token survives.
    """
    if not settings.passthrough_enabled:
        return _error_response(
            400,
            "invalid_request_error",
            "Passthrough is disabled; only Agentaus models are available on this bridge.",
        )

    client: httpx.AsyncClient = (
        getattr(request.app.state, "anthropic_client", None) or request.app.state.client
    )
    url = f"{settings.anthropic_base_url}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = _anthropic_request_headers(request)
    _warn_if_bridge_token_was_the_only_credential(request, headers)

    # Retry the connect only. Nothing has been written to the client yet, so replaying
    # the identical body is invisible; once we start streaming the response back a
    # retry would duplicate bytes, so failures past this point are surfaced as-is.
    # Keep Content-Length: with it set, httpx sends the sliced body as a plain upload
    # rather than switching to chunked transfer encoding.
    headers["content-length"] = str(len(raw))

    upstream = None
    for attempt in range(settings.max_retries + 1):
        upstream_request = client.build_request(
            request.method, url, headers=headers, content=_upload_slices(raw)
        )
        try:
            attempt_started = time.monotonic()
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            if not _is_retryable_exception(exc) or attempt == settings.max_retries:
                return _error_response(
                    502, "api_error", f"Upstream Anthropic request failed: {_describe(exc)}"
                )
            await _sleep_before_retry(
                attempt, _describe(exc), time.monotonic() - attempt_started)
            continue

        # Anthropic's own retry policy, not the Agentaus one: 402 is a billing error
        # here and goes straight back to the client with its body untouched.
        if upstream.status_code in _PASSTHROUGH_RETRYABLE_STATUS and attempt < settings.max_retries:
            await upstream.aclose()
            await _sleep_before_retry(
                attempt, f"HTTP {upstream.status_code}",
                time.monotonic() - attempt_started)
            continue
        break

    if upstream is None:  # pragma: no cover - loop always assigns or returns
        return _error_response(502, "api_error", "Upstream Anthropic request failed")

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _STRIP_RESPONSE_HEADERS
    }

    is_event_stream = "text/event-stream" in (upstream.headers.get("content-type") or "")

    async def body_iterator() -> AsyncIterator[bytes]:
        try:
            # aiter_bytes() decompresses as it streams. aiter_raw() would hand back the
            # still-gzipped body, and since we strip `content-encoding` below the client
            # would try to parse compressed bytes as JSON ("Failed to parse JSON").
            # Small responses are often uncompressed, so the raw variant fails only on
            # the larger ones - which is why this looked intermittent.
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except httpx.HTTPError as exc:
            # Mid-stream, so it cannot be retried: bytes have already gone to the client.
            # Letting it escape crashed the ASGI app with a traceback and dropped the
            # socket without a terminating chunk. An SSE error event ends the stream in a
            # form Claude Code reports and recovers from.
            rlog(logging.WARNING, "passthrough stream failed mid-response: %s",
                 _describe(exc))
            if is_event_stream:
                yield AnthropicStreamBuilder.error(
                    f"Connection to Anthropic failed mid-response ({_describe(exc)}). "
                    "Retry the turn.", "api_error",
                )
        finally:
            await upstream.aclose()

    rlog(logging.INFO, "passthrough -> %s %d", url.split("//", 1)[-1][:60],
         upstream.status_code)

    return StreamingResponse(
        body_iterator(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )

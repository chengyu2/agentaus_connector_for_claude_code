"""Requests for Claude models, forwarded to api.anthropic.com unchanged."""

from __future__ import annotations

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
_STRIP_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
}


# --------------------------------------------------------------------------------------
# Anthropic passthrough
# --------------------------------------------------------------------------------------

async def _passthrough(request: Request, raw: bytes) -> Response:
    """Forward a request to api.anthropic.com unchanged.

    Headers go through verbatim: `anthropic-beta` carries the OAuth capability that a
    claude.ai subscription login needs, and error bodies must stay unmodified or
    Claude Code's capability-rejection retry logic stops matching on them.
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

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _STRIP_REQUEST_HEADERS
    }

    # Retry the connect only. Nothing has been written to the client yet, so replaying
    # the identical body is invisible; once we start streaming the response back a
    # retry would duplicate bytes, so failures past this point are surfaced as-is.
    upstream = None
    for attempt in range(settings.max_retries + 1):
        upstream_request = client.build_request(
            request.method, url, headers=headers, content=raw
        )
        try:
            attempt_started = time.monotonic()
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            if not _is_retryable_exception(exc) or attempt == settings.max_retries:
                return _error_response(
                    502, "api_error", f"Upstream Anthropic request failed: {exc}"
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

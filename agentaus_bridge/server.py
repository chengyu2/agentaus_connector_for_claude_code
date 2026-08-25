"""HTTP surface of the bridge: an Anthropic Messages API that Claude Code can talk to.

Requests are routed per-request by model id:
  * model contains "agentaus"  -> translated and sent to the Agentaus API
  * anything else              -> forwarded byte-for-byte to api.anthropic.com

That per-request split is what lets Agentaus sit *alongside* the built-in Claude
models in one session instead of replacing them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Callable

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import settings
from .translate import (
    AnthropicStreamBuilder,
    ToolCallAccumulator,
    agentaus_response_to_anthropic,
    anthropic_request_to_agentaus,
    estimate_request_tokens,
    sse,
)

log = logging.getLogger("agentaus-bridge")

app = FastAPI(title="Agentaus bridge for Claude Code", docs_url=None, redoc_url=None)

# Headers we must not copy verbatim when proxying (hop-by-hop or recomputed).
_STRIP_REQUEST_HEADERS = {
    "host", "content-length", "connection", "transfer-encoding", "accept-encoding",
}
_STRIP_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
}


@app.on_event("startup")
async def _startup() -> None:
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.read_timeout,
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.connect_timeout,
        ),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        follow_redirects=False,
    )
    log.info(
        "bridge ready  agentaus=%s  passthrough=%s -> %s  markers=%s",
        settings.agentaus_url,
        "on" if settings.passthrough_enabled else "off",
        settings.anthropic_base_url,
        ",".join(settings.agentaus_model_markers),
    )
    if not settings.agentaus_api_key:
        log.warning("AGENTAUS_API_KEY is not set - Agentaus requests will fail with 401")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await app.state.client.aclose()


# --------------------------------------------------------------------------------------
# Small endpoints Claude Code probes at startup
# --------------------------------------------------------------------------------------

@app.api_route("/api/hello", methods=["GET", "HEAD"])
async def hello() -> Response:
    """Claude Code's connection-warming probe."""
    return Response(status_code=200)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "agentaus_url": settings.agentaus_url,
            "agentaus_key_configured": bool(settings.agentaus_api_key),
            "passthrough": settings.passthrough_enabled,
            "anthropic_upstream": settings.anthropic_base_url,
            "model_markers": settings.agentaus_model_markers,
        }
    )


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """Served for completeness. Note Claude Code's gateway model discovery only keeps
    ids containing "claude" or "anthropic", so use ANTHROPIC_CUSTOM_MODEL_OPTION to put
    Agentaus in the /model picker instead of relying on discovery."""
    return JSONResponse(
        {
            "data": [
                {
                    "id": "agentaus",
                    "display_name": "Agentaus (Trellis Data)",
                    "type": "model",
                    "created_at": "2026-05-01T00:00:00Z",
                }
            ],
            "has_more": False,
        }
    )


# --------------------------------------------------------------------------------------
# Auth guard (optional, for non-localhost deployments)
# --------------------------------------------------------------------------------------

def _client_authorised(request: Request) -> bool:
    if not settings.bridge_token:
        return True
    supplied = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        supplied = supplied or auth[7:]
    return supplied == settings.bridge_token


# --------------------------------------------------------------------------------------
# Main entrypoint
# --------------------------------------------------------------------------------------

@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    if not _client_authorised(request):
        return _error_response(401, "authentication_error", "Invalid bridge token")

    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        return _error_response(400, "invalid_request_error", f"Malformed JSON body: {exc}")

    model = body.get("model") or ""
    wants_stream = bool(body.get("stream"))
    to_agentaus = settings.routes_to_agentaus(model) or not settings.passthrough_enabled

    if settings.log_bodies:
        log.info("request body: %s", json.dumps(body)[:4000])

    if to_agentaus:
        return await _handle_agentaus(request, body, model, wants_stream)
    return await _passthrough(request, raw)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Response:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        body = {}
    model = body.get("model") or ""
    if settings.routes_to_agentaus(model) or not settings.passthrough_enabled:
        # Agentaus exposes no tokenizer, so this is a char/4 estimate. It only feeds
        # Claude Code's context meter and auto-compact trigger, not billing.
        return JSONResponse({"input_tokens": estimate_request_tokens(body)})
    return await _passthrough(request, raw)


# --------------------------------------------------------------------------------------
# Agentaus path
# --------------------------------------------------------------------------------------

def _agentaus_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.agentaus_api_key}",
        "Content-Type": "application/json",
        # Agentaus answers 406 to an explicit "Accept: text/event-stream", so ask for
        # anything and let the `stream` body flag decide the response format.
        "Accept": "*/*",
    }


async def _handle_agentaus(
    request: Request, body: dict, model: str, wants_stream: bool
) -> Response:
    payload = anthropic_request_to_agentaus(
        body,
        system_prompt_overwrite=settings.system_prompt_overwrite,
        stream=settings.upstream_stream and wants_stream,
    )
    client: httpx.AsyncClient = request.app.state.client
    started = time.monotonic()
    display_model = model or "agentaus"

    if settings.log_bodies:
        log.info("-> agentaus payload: %s", json.dumps(payload)[:4000])

    if not wants_stream:
        try:
            upstream = await client.post(
                settings.agentaus_url, json={**payload, "stream": False}, headers=_agentaus_headers()
            )
        except httpx.HTTPError as exc:
            return _error_response(502, "api_error", f"Agentaus request failed: {exc}")

        if upstream.status_code >= 400:
            return _upstream_error_response(upstream.status_code, upstream.text)

        try:
            data = upstream.json()
        except ValueError:
            return _error_response(502, "api_error", "Agentaus returned a non-JSON response")

        message = agentaus_response_to_anthropic(data, model=display_model)
        log.info(
            "POST /v1/messages model=%s route=agentaus stream=false -> 200 in %.1fs in=%s out=%s",
            display_model,
            time.monotonic() - started,
            message["usage"]["input_tokens"],
            message["usage"]["output_tokens"],
        )
        return JSONResponse(message)

    generator = _keepalive(
        _agentaus_event_stream(client, payload, body, display_model, started),
        settings.ping_interval_seconds,
        AnthropicStreamBuilder.ping,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _agentaus_event_stream(
    client: httpx.AsyncClient,
    payload: dict,
    original: dict,
    model: str,
    started: float,
) -> AsyncIterator[bytes]:
    """Produce a valid Anthropic SSE stream from an Agentaus response."""
    builder = AnthropicStreamBuilder(
        model,
        input_tokens=estimate_request_tokens(original),
        chunk_chars=settings.chunk_chars,
    )
    yield builder.start()

    finish_reason: str | None = None
    usage: dict | None = None
    accumulator = ToolCallAccumulator()

    try:
        if payload.get("stream"):
            async with client.stream(
                "POST", settings.agentaus_url, json=payload, headers=_agentaus_headers()
            ) as upstream:
                if upstream.status_code >= 400:
                    detail = (await upstream.aread()).decode("utf-8", "replace")[:500]
                    yield builder.error(
                        f"Agentaus returned HTTP {upstream.status_code}: {detail}",
                        _error_type_for_status(upstream.status_code),
                    )
                    yield builder.finish("stop", None)
                    return

                async for line in upstream.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            yield builder.text(text)
                        if delta.get("tool_calls"):
                            accumulator.add(delta["tool_calls"])
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
        else:
            upstream = await client.post(
                settings.agentaus_url,
                json={**payload, "stream": False},
                headers=_agentaus_headers(),
            )
            if upstream.status_code >= 400:
                yield builder.error(
                    f"Agentaus returned HTTP {upstream.status_code}: {upstream.text[:500]}",
                    _error_type_for_status(upstream.status_code),
                )
                yield builder.finish("stop", None)
                return
            data = upstream.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            if message.get("content"):
                yield builder.text(message["content"])
            accumulator.add(
                [{"index": i, **call} for i, call in enumerate(message.get("tool_calls") or [])]
            )
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage")

        for call in accumulator.drain():
            yield builder.tool_use(call["id"], call["name"], call["arguments"])

        yield builder.finish(finish_reason, usage)
        log.info(
            "POST /v1/messages model=%s route=agentaus stream=true -> 200 in %.1fs finish=%s usage=%s",
            model,
            time.monotonic() - started,
            finish_reason,
            usage,
        )
    except httpx.HTTPError as exc:
        log.warning("agentaus stream failed: %s", exc)
        yield builder.error(f"Agentaus request failed: {exc}", "api_error")
        yield builder.finish("stop", None)


async def _keepalive(
    source: AsyncIterator[bytes], interval: float, make_ping: Callable[[], bytes]
) -> AsyncIterator[bytes]:
    """Emit ping events while `source` is silent.

    Claude Code aborts any stream that sends no bytes for 300 seconds, and Agentaus
    sends nothing at all until the whole completion is ready. Without these pings a
    long generation looks like a dead connection.
    """
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def pump() -> None:
        try:
            async for item in source:
                await queue.put(item)
        except Exception as exc:  # surfaced to the consumer below
            await queue.put(exc)
        finally:
            await queue.put(sentinel)

    pump_task = asyncio.create_task(pump())
    getter: asyncio.Task | None = None
    try:
        while True:
            if getter is None:
                getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter}, timeout=interval)
            if not done:
                yield make_ping()
                continue
            item = getter.result()
            getter = None
            if item is sentinel:
                return
            if isinstance(item, BaseException):
                log.warning("stream producer error: %s", item)
                yield AnthropicStreamBuilder.error(str(item), "api_error")
                return
            yield item
    finally:
        if getter is not None:
            getter.cancel()
        pump_task.cancel()


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

    client: httpx.AsyncClient = request.app.state.client
    url = f"{settings.anthropic_base_url}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _STRIP_REQUEST_HEADERS
    }

    upstream_request = client.build_request(
        request.method, url, headers=headers, content=raw
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        return _error_response(502, "api_error", f"Upstream Anthropic request failed: {exc}")

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _STRIP_RESPONSE_HEADERS
    }

    async def body_iterator() -> AsyncIterator[bytes]:
        try:
            # aiter_bytes() decompresses as it streams. aiter_raw() would hand back the
            # still-gzipped body, and since we strip `content-encoding` below the client
            # would try to parse compressed bytes as JSON ("Failed to parse JSON").
            # Small responses are often uncompressed, so the raw variant fails only on
            # the larger ones - which is why this looked intermittent.
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iterator(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


# --------------------------------------------------------------------------------------
# Error helpers
# --------------------------------------------------------------------------------------

def _error_type_for_status(status: int) -> str:
    if status in (401, 403):
        return "authentication_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "api_error"
    return "invalid_request_error"


def _error_response(status: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"type": "error", "error": {"type": error_type, "message": message}},
        status_code=status,
    )


def _upstream_error_response(status: int, text: str) -> JSONResponse:
    return _error_response(
        status, _error_type_for_status(status), f"Agentaus error: {text[:500]}"
    )

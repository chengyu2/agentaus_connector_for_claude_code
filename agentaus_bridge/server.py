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
import random
import re
import time
import uuid
from typing import AsyncIterator, Callable

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .augment import (
    REVIEW_INSTRUCTION,
    REVISE_INSTRUCTION,
    review_says_ok,
    with_guidance,
    worth_reviewing,
)
from .compact import ConversationCompactor
from .tokens import calibrator, has_tokeniser as _has_tokeniser
from .config import settings
from .translate import (
    AnthropicStreamBuilder,
    ToolCallAccumulator,
    agentaus_response_to_anthropic,
    anthropic_request_to_agentaus,
    estimate_request_tokens,
    raw_token_count,
    sse,
    trim_messages_to_fit,
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
            "context_limit": _context_limit(),
            "tokeniser": "tiktoken" if _has_tokeniser() else "chars/4 fallback",
            "token_calibration": {
                "ratio": round(calibrator.ratio, 3),
                "samples": calibrator.samples,
            },
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
# Transient-failure retries
# --------------------------------------------------------------------------------------

# Status codes worth a second attempt. These are gateway/capacity signals rather than
# anything about the request itself, so replaying the identical body is safe.
# 520-527 are Cloudflare's own errors; Agentaus sits behind it and 524 (origin
# timed out) is common on a long summarisation request.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504,
                     520, 521, 522, 523, 524, 525, 526, 527}

# Connection-level faults that never reached the application upstream. A DNS blip
# ("nodename nor servname provided") arrives as ConnectError; a dropped keep-alive
# socket arrives as RemoteProtocolError or ConnectError depending on timing.
_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def _is_retryable_exception(exc: BaseException) -> bool:
    return isinstance(exc, _RETRYABLE_EXCEPTIONS)


def _describe(exc: BaseException) -> str:
    """Type plus message. The type alone cannot distinguish a DNS failure from a
    refused connection, which is the first thing you need when reading the log."""
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped so a long outage still ends the turn.

    Jitter matters when several requests fail at once - Claude Code fires background
    calls alongside the main turn, and without it they would all retry in lockstep and
    re-hammer an upstream that is already struggling.
    """
    base = settings.retry_backoff_seconds * (2**attempt)
    return min(base, settings.retry_max_delay_seconds) + random.uniform(
        0, settings.retry_backoff_seconds
    )


async def _sleep_before_retry(attempt: int, why: str) -> None:
    delay = _retry_delay(attempt)
    log.warning("upstream attempt %d failed (%s); retrying in %.2fs", attempt + 1, why, delay)
    await asyncio.sleep(delay)


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, *, json_body: dict, headers: dict
) -> httpx.Response:
    """POST, retrying transient connection faults and gateway status codes.

    Only used for buffered (non-streaming) requests, where replaying costs nothing
    because no bytes have been handed to the client yet.
    """
    last_exc: Exception | None = None
    for attempt in range(settings.max_retries + 1):
        try:
            response = await client.post(url, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            last_exc = exc
            if not _is_retryable_exception(exc) or attempt == settings.max_retries:
                raise
            await _sleep_before_retry(attempt, _describe(exc))
            continue

        if response.status_code in _RETRYABLE_STATUS and attempt < settings.max_retries:
            await response.aread()  # release the connection before sleeping
            await _sleep_before_retry(attempt, f"HTTP {response.status_code}")
            continue
        return response

    raise last_exc if last_exc else RuntimeError("retry loop exited without a response")


# --------------------------------------------------------------------------------------
# Agentaus path
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Context window
# --------------------------------------------------------------------------------------

# Agentaus states its own limit when a prompt overflows:
#   "The engine prompt length 224662 exceeds the max_model_len 131072"
# so the bridge learns the number from the API instead of trusting a constant that
# goes stale the moment Trellis Data changes the model.
_MAX_MODEL_LEN = re.compile(r"max_model_len\s+(\d+)")
_learned_limit: int | None = None


def _reset_learned_limit() -> None:
    """Clear the learned window. Used by tests: it is module state that would
    otherwise leak between cases and make results depend on ordering."""
    global _learned_limit
    _learned_limit = None


def _learn_limit_from(message: str) -> None:
    global _learned_limit
    match = _MAX_MODEL_LEN.search(message or "")
    if not match:
        return
    value = int(match.group(1))
    if value > 0 and value != _learned_limit:
        _learned_limit = value
        log.info("learned Agentaus context window from the API: %d tokens", value)


def _context_limit() -> int:
    """The window to enforce.

    An explicitly configured AGENTAUS_MAX_INPUT_TOKENS always wins - learning a value
    from an error must never quietly override what the operator asked for. Otherwise
    prefer what Agentaus itself reported over the compiled-in default.
    """
    if settings.max_input_tokens_is_explicit:
        return settings.agentaus_max_input_tokens
    return _learned_limit or settings.agentaus_max_input_tokens


async def _agentaus_summarise(client: httpx.AsyncClient, text: str) -> str:
    """Ask Agentaus to compact a slice of the conversation.

    Deliberately delegated to the model rather than to heuristics here: what is worth
    keeping out of an engineering conversation is a judgement call, and the model is
    better placed to make it than any rule the bridge could hard-code.
    """
    payload = {
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "system_prompt_overwrite": True,
    }
    response = await _post_with_retry(
        client, settings.agentaus_url, json_body=payload, headers=_agentaus_headers()
    )
    if response.status_code >= 400:
        raise RuntimeError(f"summariser returned HTTP {response.status_code}")
    data = response.json()
    if isinstance(data.get("error"), dict):
        _learn_limit_from(str(data["error"].get("message") or ""))
        raise RuntimeError(str(data["error"].get("message"))[:200])
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


async def _self_review(client: httpx.AsyncClient, request_text: str, answer: str) -> str:
    """Have Agentaus critique its own answer and revise it if defects are found.

    Returns the answer to use - the original when the review is clean, or a revision.
    Any failure returns the original: a broken review must never lose a good answer.
    """
    try:
        review = await _agentaus_summarise(
            client, REVIEW_INSTRUCTION.format(request=request_text[:12000], answer=answer[:12000])
        )
        if review_says_ok(review):
            return answer
        revised = await _agentaus_summarise(
            client,
            REVISE_INSTRUCTION.format(
                request=request_text[:12000], answer=answer[:12000], defects=review[:6000]
            ),
        )
        if revised and revised.strip():
            log.info("self-review revised the answer (%d -> %d chars)", len(answer), len(revised))
            return revised.strip()
    except Exception as exc:
        log.warning("self-review failed (%s); keeping the original answer", exc)
    return answer


def _last_user_text(body: dict) -> str:
    """The most recent user message, as the request the review judges against."""
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if parts:
                return "\n".join(parts)
    return ""


def _get_compactor(request: Request) -> ConversationCompactor:
    """One compactor per process, so its summary cache survives between turns.

    Without the cache the same conversation prefix would be re-summarised on every
    turn, since Claude Code re-sends the whole history each time.
    """
    existing = getattr(request.app.state, "compactor", None)
    if existing is None:
        client = request.app.state.client
        existing = ConversationCompactor(
            lambda text: _agentaus_summarise(client, text),
            max_concurrency=settings.agentaus_summary_concurrency,
            verify=settings.agentaus_verify_summary,
        )
        request.app.state.compactor = existing
    return existing


def _calibrate_from_usage(body: dict, usage: dict | None) -> None:
    """Compare our count of a request against the count Agentaus charged for it.

    This is the only ground truth available: Agentaus does not publish its tokeniser,
    so the ratio between our count and its reported `input_tokens` is what makes the
    context arithmetic converge on reality.
    """
    if not isinstance(usage, dict):
        return
    reported = usage.get("input_tokens")
    if not isinstance(reported, int) or reported <= 0:
        return
    counted = raw_token_count(
        json.dumps(
            {"s": body.get("system"), "m": body.get("messages"), "t": body.get("tools")},
            default=str,
        )
    )
    calibrator.observe(counted, reported)


def _with_summary(system, summary: str, replaced: int):
    """Fold the summary into the system prompt, replacing the messages it stands for."""
    notice = (
        f"\n\n[Earlier conversation summary - the {replaced} oldest messages were "
        f"replaced by this compacted record to fit the context window. Treat it as an "
        f"accurate account of what happened before; if the user refers to something "
        f"not covered here, say so rather than guessing.]\n{summary}"
    )
    if system is None:
        return notice.strip()
    if isinstance(system, str):
        return system + notice
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": notice.strip()}]
    return system


def _with_trim_notice(system, dropped: int):
    """Append a note to the system prompt saying history was trimmed.

    Without this the model answers as though it can see the whole conversation and
    may invent continuity it has no basis for.
    """
    notice = (
        f"\n\n[Note: the {dropped} oldest message(s) of this conversation were removed "
        f"to fit the context window. If the user refers to something earlier that you "
        f"cannot see, say so rather than guessing.]"
    )
    if system is None:
        return notice.strip()
    if isinstance(system, str):
        return system + notice
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": notice.strip()}]
    return system


def _agentaus_error_text(err: dict) -> str:
    """Flatten an Agentaus error object into a message worth showing a user.

    The context-length failure reads "The engine prompt length N exceeds the
    max_model_len M", which is actionable once it actually reaches the client.
    """
    message = str(err.get("message") or err) if err else "unknown Agentaus error"
    _learn_limit_from(message)
    if "max_model_len" in message or "exceeds" in message:
        # Prefix Anthropic's canonical over-length wording so Claude Code's
        # auto-compact-and-retry path recognises it, then keep the upstream text.
        return (
            "prompt is too long: this conversation exceeds Agentaus' context window. "
            "To recover, switch to a Claude model with /model opus and run /compact "
            "there, then switch back; /compact on Agentaus fails the same way while "
            "the conversation is over the window. /clear also works but loses history. "
            f"(Agentaus said: {message})"
        )
    return f"Agentaus error: {message}"


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
    client: httpx.AsyncClient = request.app.state.client
    started = time.monotonic()
    display_model = model or "agentaus"

    # Pre-flight context check. Agentaus counts the prompt and the reply against one
    # window, so reserve the requested output before comparing. Catching it here turns
    # a confusing upstream failure into an actionable message and saves sending a
    # payload that can be hundreds of kilobytes.
    limit = _context_limit()
    if limit > 0:
        estimated = estimate_request_tokens(body)
        original_estimate = estimated
        reserved = int(body.get("max_tokens") or 0)
        threshold = settings.agentaus_compact_threshold
        needs_compaction = estimated + reserved > int(limit * threshold)
        if needs_compaction and settings.agentaus_auto_trim:
            plan = await _get_compactor(request).compact(
                body,
                limit=limit,
                reserve=reserved,
                keep_fraction=settings.agentaus_keep_fraction,
                threshold=threshold,
                chunk_tokens=settings.agentaus_summary_chunk_tokens,
            )
            if plan["method"] == "summarised":
                body = {**body, "messages": plan["messages"]}
                body["system"] = _with_summary(
                    body.get("system"), plan["summary"], plan["summarised"]
                )
                estimated = estimate_request_tokens(body)
                log.warning(
                    "compacted %d oldest message(s) into a summary to fit the %d-token "
                    "window (~%d -> ~%d tokens)",
                    plan["summarised"], limit, original_estimate, estimated,
                )
            elif plan["method"] == "trimmed":
                body = {**body, "messages": plan["messages"]}
                body["system"] = _with_trim_notice(body.get("system"), plan["dropped"])
                estimated = estimate_request_tokens(body)
                log.warning(
                    "summarisation could not fit the window; dropped %d oldest "
                    "message(s) instead (~%d -> ~%d tokens)",
                    plan["dropped"], original_estimate, estimated,
                )

        if estimated + reserved > limit:
            log.warning(
                "rejected oversized request: ~%d prompt + %d reserved > %d limit",
                estimated, reserved, limit,
            )
            return _error_response(
                400,
                "invalid_request_error",
                # Lead with Anthropic's canonical wording. Claude Code matches on
                # "prompt is too long" to trigger auto-compact and retry, so phrasing
                # it this way turns a dead turn into automatic recovery. The
                # Agentaus-specific detail follows for anyone reading the log.
                # Recovery advice is ordered deliberately. /compact is NOT first:
                # compaction works by sending the conversation to the model to be
                # summarised, so once the conversation is already over the window the
                # compaction call is over it too and fails identically - a deadlock
                # (anthropics/claude-code#25867). Switching to a Claude model is the
                # reliable escape, and it exists only because this bridge keeps both
                # providers live in the same session.
                f"prompt is too long: {estimated + reserved} tokens > {limit} maximum. "
                f"Agentaus has a {limit:,}-token context window (roughly "
                f"{estimated:,} prompt + {reserved:,} reserved for the reply). "
                f"To recover: switch to a Claude model with /model opus - it has a much "
                f"larger window, so /compact will succeed there, and you can switch back "
                f"to Agentaus afterwards. Otherwise /clear starts fresh. Note /compact "
                f"on Agentaus will fail the same way while you are this far over, "
                f"because compaction must itself fit in the window. "
                f"(Set AGENTAUS_MAX_INPUT_TOKENS to change this limit.)",
            )

    # Supplement the system prompt for Agentaus only. Claude turns never reach here -
    # they are forwarded untouched by _passthrough.
    if settings.agentaus_guidance:
        body = {**body, "system": with_guidance(body.get("system"))}

    # Built only after the context guard, so any trimming above is reflected in what
    # actually gets sent. Building it earlier silently discarded the trim.
    payload = anthropic_request_to_agentaus(
        body,
        system_prompt_overwrite=settings.system_prompt_overwrite,
        stream=settings.upstream_stream and wants_stream,
    )

    if settings.log_bodies:
        log.info("-> agentaus payload: %s", json.dumps(payload)[:4000])

    if not wants_stream:
        try:
            upstream = await _post_with_retry(
                client,
                settings.agentaus_url,
                json_body={**payload, "stream": False},
                headers=_agentaus_headers(),
            )
        except httpx.HTTPError as exc:
            return _error_response(502, "api_error", f"Agentaus request failed: {exc}")

        if upstream.status_code >= 400:
            return _upstream_error_response(upstream.status_code, upstream.text)

        try:
            data = upstream.json()
        except ValueError:
            return _error_response(502, "api_error", "Agentaus returned a non-JSON response")

        # Agentaus can answer HTTP 200 with an error object instead of choices.
        if isinstance(data.get("error"), dict):
            log.warning("agentaus in-band error: %s", _agentaus_error_text(data["error"]))
            return _error_response(
                400,
                data["error"].get("type") or "api_error",
                _agentaus_error_text(data["error"]),
            )

        _calibrate_from_usage(body, data.get("usage"))

        # Review only a plain text answer. A turn that calls tools is mid-task, and
        # rewriting it would break the tool_use the client is waiting on.
        choice0 = (data.get("choices") or [{}])[0]
        msg0 = choice0.get("message") or {}
        if (
            settings.agentaus_self_review
            and not msg0.get("tool_calls")
            and worth_reviewing(msg0.get("content") or "",
                                min_chars=settings.agentaus_review_min_chars)
        ):
            reviewed = await _self_review(client, _last_user_text(body), msg0["content"])
            if reviewed != msg0["content"]:
                msg0 = {**msg0, "content": reviewed}
                data = {**data, "choices": [{**choice0, "message": msg0}]
                        + list((data.get("choices") or [])[1:])}

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

    # A transient fault before the first content byte can be retried invisibly: the
    # client has only seen `message_start`, which carries no content. Once any text or
    # tool call has been emitted a retry would duplicate it, so `emitted` latches the
    # stream to fail-fast from that point on.
    # Buffer the answer when it may need revising. Tool turns are never buffered:
    # they carry no prose to review and the client is waiting on the tool_use.
    buffering = settings.agentaus_self_review
    pending: list[str] = []

    attempt = 0
    while True:
        emitted = False
        pending = []
        finish_reason = None
        usage = None
        accumulator = ToolCallAccumulator()
        retry_reason: str | None = None

        try:
            if payload.get("stream"):
                async with client.stream(
                    "POST", settings.agentaus_url, json=payload, headers=_agentaus_headers()
                ) as upstream:
                    if upstream.status_code >= 400:
                        detail = (await upstream.aread()).decode("utf-8", "replace")[:500]
                        if (
                            upstream.status_code in _RETRYABLE_STATUS
                            and attempt < settings.max_retries
                        ):
                            retry_reason = f"HTTP {upstream.status_code}"
                        else:
                            yield builder.error(
                                f"Agentaus returned HTTP {upstream.status_code}: {detail}",
                                _error_type_for_status(upstream.status_code),
                            )
                            yield builder.finish("stop", None)
                            return
                    else:
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

                            # Agentaus reports some failures - an over-length
                            # prompt among them - as HTTP 200 with an error object
                            # inside the SSE body. Nothing here has "choices", so
                            # without this branch the error is skipped and the turn
                            # ends as an empty, successful-looking message.
                            if isinstance(chunk.get("error"), dict):
                                yield builder.error(
                                    _agentaus_error_text(chunk["error"]),
                                    chunk["error"].get("type") or "api_error",
                                )
                                yield builder.finish("stop", None)
                                log.warning(
                                    "agentaus in-band error: %s",
                                    _agentaus_error_text(chunk["error"]),
                                )
                                return

                            for choice in chunk.get("choices") or []:
                                delta = choice.get("delta") or {}
                                text = delta.get("content")
                                if text:
                                    if buffering:
                                        # Held back rather than streamed: an answer
                                        # already on screen cannot be revised. Agentaus
                                        # sends its reply in one piece anyway, so this
                                        # costs little in practice.
                                        pending.append(text)
                                    else:
                                        emitted = True
                                        yield builder.text(text)
                                if delta.get("tool_calls"):
                                    emitted = True
                                    accumulator.add(delta["tool_calls"])
                                if choice.get("finish_reason"):
                                    finish_reason = choice["finish_reason"]
                            if chunk.get("usage"):
                                usage = chunk["usage"]
            else:
                upstream = await _post_with_retry(
                    client,
                    settings.agentaus_url,
                    json_body={**payload, "stream": False},
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
                if isinstance(data.get("error"), dict):
                    yield builder.error(
                        _agentaus_error_text(data["error"]),
                        data["error"].get("type") or "api_error",
                    )
                    yield builder.finish("stop", None)
                    return
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                if message.get("content"):
                    if buffering:
                        pending.append(message["content"])
                    else:
                        emitted = True
                        yield builder.text(message["content"])
                accumulator.add(
                    [{"index": i, **call} for i, call in enumerate(message.get("tool_calls") or [])]
                )
                finish_reason = choice.get("finish_reason")
                usage = data.get("usage")

        except httpx.HTTPError as exc:
            if emitted or not _is_retryable_exception(exc) or attempt >= settings.max_retries:
                log.warning("agentaus stream failed: %s", exc)
                yield builder.error(f"Agentaus request failed: {exc}", "api_error")
                yield builder.finish("stop", None)
                return
            retry_reason = _describe(exc)

        if retry_reason:
            await _sleep_before_retry(attempt, retry_reason)
            attempt += 1
            continue
        break

    answer = "".join(pending)
    if buffering and answer:
        if not accumulator.pending() and worth_reviewing(
            answer, min_chars=settings.agentaus_review_min_chars
        ):
            answer = await _self_review(client, _last_user_text(original), answer)
        yield builder.text(answer)

    for call in accumulator.drain():
        yield builder.tool_use(call["id"], call["name"], call["arguments"])

    _calibrate_from_usage(original, usage)
    yield builder.finish(finish_reason, usage)
    log.info(
        "POST /v1/messages model=%s route=agentaus stream=true -> 200 in %.1fs finish=%s usage=%s",
        model,
        time.monotonic() - started,
        finish_reason,
        usage,
    )


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

    # Retry the connect only. Nothing has been written to the client yet, so replaying
    # the identical body is invisible; once we start streaming the response back a
    # retry would duplicate bytes, so failures past this point are surfaced as-is.
    upstream = None
    for attempt in range(settings.max_retries + 1):
        upstream_request = client.build_request(
            request.method, url, headers=headers, content=raw
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            if not _is_retryable_exception(exc) or attempt == settings.max_retries:
                return _error_response(
                    502, "api_error", f"Upstream Anthropic request failed: {exc}"
                )
            await _sleep_before_retry(attempt, _describe(exc))
            continue

        if upstream.status_code in _RETRYABLE_STATUS and attempt < settings.max_retries:
            await upstream.aclose()
            await _sleep_before_retry(attempt, f"HTTP {upstream.status_code}")
            continue
        break

    if upstream is None:  # pragma: no cover - loop always assigns or returns
        return _error_response(502, "api_error", "Upstream Anthropic request failed")

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

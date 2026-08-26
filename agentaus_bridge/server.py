"""HTTP surface of the bridge: an Anthropic Messages API that Claude Code can talk to.

Requests are routed per-request by model id:
  * model contains "agentaus"  -> translated and sent to the Agentaus API
  * anything else              -> forwarded byte-for-byte to api.anthropic.com

That per-request split is what lets Agentaus sit *alongside* the built-in Claude
models in one session instead of replacing them.
"""

from __future__ import annotations

import asyncio
import contextvars
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
    ADJUDICATE_INSTRUCTION,
    REVIEW_INSTRUCTION,
    REVISE_INSTRUCTION,
    declared_verdict,
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

    _request_id.set(_new_request_id())
    messages_in = len(body.get("messages") or [])
    rlog(
        logging.INFO,
        "recv model=%s route=%s stream=%s msgs=%d est=%d bytes=%d",
        model or "(none)",
        "agentaus" if to_agentaus else "anthropic",
        wants_stream,
        messages_in,
        estimate_request_tokens(body),
        len(raw),
    )

    if settings.log_bodies:
        rlog(logging.INFO, "request body: %s", json.dumps(body)[:4000])

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
# Request-scoped logging
# --------------------------------------------------------------------------------------

# Every line for one turn carries the same short id. Without it a long request is
# indistinguishable from the concurrent summarisation calls it spawns, which is exactly
# what made a 90-second stall hard to read: sixty identical "200 OK" lines and no way
# to tell which was the actual turn.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def _new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def rlog(level: int, message: str, *args) -> None:
    """Log with the current request id prefixed."""
    log.log(level, "req %s " + message, _request_id.get(), *args)


class _Phase:
    """Times a phase and logs its start and end.

    Start lines matter as much as end lines: a phase that never finishes only shows up
    in the log as a start with no matching end, and that is the signature of a hang.
    """

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        self.started = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> "_Phase":
        self.started = time.monotonic()
        rlog(logging.INFO, "%s start%s", self.name, f" ({self.detail})" if self.detail else "")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.elapsed = time.monotonic() - self.started
        if exc_type is not None:
            # CancelledError and GeneratorExit carry no message, so name the type -
            # "FAILED: " with nothing after it says less than it should.
            reason = str(exc).strip() or exc_type.__name__
            rlog(logging.WARNING, "%s ended after %.1fs: %s", self.name, self.elapsed, reason)
        else:
            rlog(logging.INFO, "%s done in %.1fs", self.name, self.elapsed)


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
    rlog(logging.WARNING, "upstream attempt %d failed (%s); retrying in %.2fs", attempt + 1, why, delay)
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
        rlog(logging.INFO, "learned Agentaus context window from the API: %d tokens", value)


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
    started_at = time.monotonic()
    response = await _post_with_retry(
        client, settings.agentaus_url, json_body=payload, headers=_agentaus_headers()
    )
    if response.status_code >= 400:
        rlog(logging.WARNING, "helper call failed HTTP %d after %.1fs",
             response.status_code, time.monotonic() - started_at)
        raise RuntimeError(f"summariser returned HTTP {response.status_code}")
    rlog(logging.DEBUG, "helper call ok in %.1fs (%d chars in)",
         time.monotonic() - started_at, len(text))
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
        verdict = declared_verdict(review)
        if verdict is None:
            # The reviewer did not follow the format. Ask it rather than guessing from
            # the prose: sniffing for "OK" misreads both "OK, but the empty case is
            # broken" and a bare approval wrapped in markdown, and the two mistakes
            # fail in opposite directions.
            adjudication = await _agentaus_summarise(
                client, ADJUDICATE_INSTRUCTION.format(review=review[:6000])
            )
            verdict = not adjudication.strip().upper().startswith("YES")
            rlog(logging.INFO, "review verdict was unstated; adjudicated as %s",
                     "sound" if verdict else "defective")
        if verdict:
            return answer
        revised = await _agentaus_summarise(
            client,
            REVISE_INSTRUCTION.format(
                request=request_text[:12000], answer=answer[:12000], defects=review[:6000]
            ),
        )
        if revised and revised.strip():
            rlog(logging.INFO, "self-review revised the answer (%d -> %d chars)", len(answer), len(revised))
            return revised.strip()
    except Exception as exc:
        rlog(logging.WARNING, "self-review failed (%s); keeping the original answer", exc)
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
            block=settings.agentaus_compact_block,
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


async def _fit_to_window(
    request: Request, body: dict, *, limit: int, reserve: int, scale: float
) -> dict:
    """Compact `body` until it should fit `limit * scale`, and return it.

    `scale` tightens the target on a retry: if Agentaus rejected the request as too
    long, our estimate was optimistic, so the next attempt aims lower rather than
    trusting the same arithmetic twice.

    Never raises and never refuses - the worst case returns the body compacted as far
    as it can be, and lets the API decide.
    """
    if limit <= 0 or not settings.agentaus_auto_trim:
        return body

    target = max(int(limit * scale), 1000)
    threshold = settings.agentaus_compact_threshold
    estimated = estimate_request_tokens(body)
    if estimated + reserve <= int(target * threshold):
        return body

    plan = await _get_compactor(request).compact(
        body,
        limit=target,
        reserve=reserve,
        keep_fraction=settings.agentaus_keep_fraction,
        threshold=threshold,
        chunk_tokens=settings.agentaus_summary_chunk_tokens,
    )
    if plan["method"] == "summarised":
        fitted = {**body, "messages": plan["messages"]}
        fitted["system"] = _with_summary(
            body.get("system"), plan["summary"], plan["summarised"]
        )
        rlog(logging.WARNING, 
            "compacted %d oldest message(s) into a summary for a %d-token target "
            "(~%d -> ~%d tokens)",
            plan["summarised"], target, estimated, estimate_request_tokens(fitted),
        )
        return fitted
    if plan["method"] == "trimmed":
        fitted = {**body, "messages": plan["messages"]}
        fitted["system"] = _with_trim_notice(body.get("system"), plan["dropped"])
        rlog(logging.WARNING, 
            "summarisation could not reach the %d-token target; dropped %d oldest "
            "message(s) instead (~%d -> ~%d tokens)",
            target, plan["dropped"], estimated, estimate_request_tokens(fitted),
        )
        return fitted
    return body


def _is_over_length(text: str) -> bool:
    """Whether an upstream error is Agentaus reporting the prompt as too long."""
    lowered = (text or "").lower()
    return "max_model_len" in lowered or "exceeds" in lowered and "prompt" in lowered


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

    # Fit the conversation to the window, then send it. Deliberately NOT a hard
    # pre-flight rejection on our own arithmetic: our token count is an estimate, and
    # refusing a request the model would have accepted is a brittle way to fail. The
    # API is the authority - if Agentaus says a prompt is too long it also states its
    # real limit, which is better evidence than anything computed here. So the estimate
    # only decides *when to compact*, a recoverable choice, and an actual rejection
    # drives a tighter retry rather than ending the turn.
    limit = _context_limit()
    reserved = int(body.get("max_tokens") or 0)
    original_estimate = estimate_request_tokens(body)

    async def prepare(current: dict, scale: float = 1.0) -> tuple[dict, dict]:
        """Fit to the window, add the guidance, and build the upstream payload.

        Returns (body, payload). Compaction can take a minute or more on a long
        conversation, which is why the streaming path runs this *inside* the response
        generator rather than before it - see the note there.
        """
        before = estimate_request_tokens(current)
        if before + reserved > int(limit * settings.agentaus_compact_threshold):
            with _Phase("compaction", f"est {before:,} tok, target {limit:,}"):
                fitted = await _fit_to_window(
                    request, current, limit=limit, reserve=reserved, scale=scale
                )
            rlog(logging.INFO, "compaction result: %d -> %d est tokens",
                 before, estimate_request_tokens(fitted))
        else:
            fitted = await _fit_to_window(
                request, current, limit=limit, reserve=reserved, scale=scale
            )
        # Supplement the system prompt for Agentaus only. Claude turns never reach
        # here - they are forwarded untouched by _passthrough.
        if settings.agentaus_guidance:
            fitted = {**fitted, "system": with_guidance(fitted.get("system"), fitted)}
        built = anthropic_request_to_agentaus(
            fitted,
            system_prompt_overwrite=settings.system_prompt_overwrite,
            stream=settings.upstream_stream and wants_stream,
        )
        if settings.log_bodies:
            rlog(logging.INFO, "-> agentaus payload: %s", json.dumps(built)[:4000])
        return fitted, built

    if not wants_stream:
        body, payload = await prepare(body)
        # Agentaus is the authority on whether a prompt fits. If it says no, it also
        # says what its real limit is, so the next attempt compacts against that
        # instead of against our estimate.
        scale = 1.0
        for fit_attempt in range(settings.agentaus_fit_attempts + 1):
            try:
                upstream = await _post_with_retry(
                    client,
                    settings.agentaus_url,
                    json_body={**payload, "stream": False},
                    headers=_agentaus_headers(),
                )
            except httpx.HTTPError as exc:
                return _error_response(502, "api_error", f"Agentaus request failed: {exc}")

            over_length = (
                upstream.status_code >= 400 and _is_over_length(upstream.text)
            )
            if not over_length or fit_attempt == settings.agentaus_fit_attempts:
                break

            _learn_limit_from(upstream.text)
            scale *= settings.agentaus_fit_shrink
            rlog(logging.WARNING, 
                "Agentaus rejected the prompt as too long; recompacting to %.0f%% "
                "of the window and retrying", scale * 100,
            )
            body = await _fit_to_window(
                request, body, limit=_context_limit(), reserve=reserved, scale=scale
            )
            payload = anthropic_request_to_agentaus(
                body,
                system_prompt_overwrite=settings.system_prompt_overwrite,
                stream=False,
            )

        if upstream.status_code >= 400:
            return _upstream_error_response(upstream.status_code, upstream.text)

        try:
            data = upstream.json()
        except ValueError:
            return _error_response(502, "api_error", "Agentaus returned a non-JSON response")

        # Agentaus can answer HTTP 200 with an error object instead of choices.
        if isinstance(data.get("error"), dict):
            rlog(logging.WARNING, "agentaus in-band error: %s", _agentaus_error_text(data["error"]))
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
        rlog(logging.INFO, 
            "POST /v1/messages model=%s route=agentaus stream=false -> 200 in %.1fs in=%s out=%s",
            display_model,
            time.monotonic() - started,
            message["usage"]["input_tokens"],
            message["usage"]["output_tokens"],
        )
        return JSONResponse(message)

    async def _refit(scale: float) -> dict:
        """Recompact against a tighter target and rebuild the upstream payload."""
        nonlocal body
        body = await _fit_to_window(
            request, body, limit=_context_limit(), reserve=reserved, scale=scale
        )
        return anthropic_request_to_agentaus(
            body,
            system_prompt_overwrite=settings.system_prompt_overwrite,
            stream=settings.upstream_stream,
        )

    # Compaction is deliberately deferred into the generator rather than done here.
    # On a long conversation it takes a minute or more, and until this function
    # returns, Claude Code has received nothing at all - no headers, no message_start,
    # and critically no keepalive pings, because the response has not begun. That
    # looks exactly like a hung request: a short "hello" on a large session produced
    # no visible response for over 90 seconds. Starting the stream first lets pings
    # flow while the summarising happens.
    async def stream_with_compaction() -> AsyncIterator[bytes]:
        try:
            prepared_body, prepared_payload = await prepare(body)

            async def refit(scale: float) -> dict:
                _, rebuilt = await prepare(prepared_body, scale)
                return rebuilt

            rlog(logging.INFO, "upstream start (est %d tok)",
                 estimate_request_tokens(prepared_body))
            async for chunk in _agentaus_event_stream(
                client, prepared_payload, prepared_body, display_model, started, refit
            ):
                yield chunk
        except asyncio.CancelledError:
            # The client hung up. Worth a line of its own: previously this produced
            # no log at all, so a turn the user abandoned looked identical to one
            # still in flight.
            rlog(logging.WARNING, "client disconnected after %.1fs",
                 time.monotonic() - started)
            raise
        except GeneratorExit:
            rlog(logging.WARNING, "stream closed early after %.1fs",
                 time.monotonic() - started)
            raise

    generator = _keepalive(
        stream_with_compaction(),
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
    refit=None,
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

    # Tracks recompaction attempts driven by Agentaus rejecting the prompt as too long.
    fit_attempt = 0
    fit_scale = 1.0

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
                        rlog(logging.WARNING,
                             "agentaus rejected the streamed request: HTTP %d %s",
                             upstream.status_code, detail)
                        if (
                            _is_over_length(detail)
                            and refit is not None
                            and fit_attempt < settings.agentaus_fit_attempts
                        ):
                            # Nothing has been emitted yet, so the request can be
                            # recompacted and replayed invisibly.
                            _learn_limit_from(detail)
                            fit_attempt += 1
                            fit_scale *= settings.agentaus_fit_shrink
                            payload = await refit(fit_scale)
                            retry_reason = f"prompt too long, refitting to {fit_scale:.0%}"
                        elif (
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
                                rlog(logging.WARNING, 
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
                rlog(logging.WARNING, "agentaus stream failed: %s", exc)
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
    rlog(logging.INFO, 
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
                rlog(logging.WARNING, "stream producer error: %s", item)
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

    rlog(logging.INFO, "passthrough -> %s %d", url.split("//", 1)[-1][:60],
         upstream.status_code)

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
    # Log the body, not just the status. httpx already logs "HTTP/1.1 400 Bad Request"
    # and stops there, so a rejection that is not about prompt length leaves no trace
    # of its cause - which is precisely the case a malformed-request bug presents as.
    rlog(logging.WARNING, "agentaus rejected the request: HTTP %d %s",
         status, (text or "")[:1000])
    return _error_response(
        status, _error_type_for_status(status), f"Agentaus error: {text[:500]}"
    )

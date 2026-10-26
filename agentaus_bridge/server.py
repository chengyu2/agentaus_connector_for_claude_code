"""HTTP surface of the bridge: an Anthropic Messages API that Claude Code can talk to.

Requests are routed per-request by model id:
  * model contains "agentaus"  -> translated and sent to the Agentaus API
  * anything else              -> forwarded byte-for-byte to api.anthropic.com

That per-request split is what lets Agentaus sit *alongside* the built-in Claude
models in one session instead of replacing them.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import random
import re
import time
import uuid
from collections import OrderedDict
from typing import AsyncIterator, Callable

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import attribution
from . import documents
from . import flight
from . import graph
from . import import_guard
from . import harmony
from . import persisted
from . import sessions
from . import prompt_style
from . import repair
from . import syntax
from . import throttle
from . import schema as tool_schema
from . import tools as bridge_tools
from .augment import (
    ADJUDICATE_INSTRUCTION,
    REVIEW_INSTRUCTION,
    REVISE_INSTRUCTION,
    declared_verdict,
    plan_prompt,
    CLASSIFY_REFUSAL_INSTRUCTION,
    GROUNDING_INSTRUCTION,
    STRIP_UNGROUNDED_INSTRUCTION,
    could_be_a_refusal,
    awaiting_background,
    early_deliverable_writes,
    writes_before_workflow,
    focus_tools,
    is_subagent_turn,
    with_library_skills,
    with_skill_descriptions,
    grounding_verdict,
    worth_grounding_check,
    read_refusal_verdict,
    read_turn_verdict,
    should_think,
    working_directory,
    working_directory_of,
    with_guidance,
    with_plan,
    with_parent_request,
    workflow_opted_in,
    worth_reviewing,
    worth_reviewing_turn,
    REFUSAL_CORRECTION,
    STALLED_CORRECTION,
)
from .compact import ConversationCompactor
from .text import normalise_for_display
from .distill import ResultDistiller
from .gate import hold
from . import ledger
from .ledger import with_ledger
from .tokens import calibrator, has_tokeniser as _has_tokeniser
from .config import settings
from .translate import (
    AnthropicStreamBuilder,
    ToolCallAccumulator,
    agentaus_response_to_anthropic,
    anthropic_request_to_agentaus,
    inject_bridge_tools,
    estimate_request_tokens,
    estimate_tokens,
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
    # Flights belong to one event loop: a test client's, or the server's.
    flight.reset()
    global _SOURCE_HASH
    _SOURCE_HASH = _source_hash()
    # Every Agentaus call takes a slot from one adaptive limit, beneath every caller.
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.read_timeout,
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.connect_timeout,
        ),
        transport=throttle.LimitedTransport(httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))),
        follow_redirects=False,
    )
    # Claude traffic gets its own pool. Sharing one meant a burst of bridge helper calls
    # - a search fan-out, a cold compaction - could hold every connection, and a Claude
    # turn then queued for a slot behind Agentaus work it has nothing to do with.
    app.state.anthropic_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.read_timeout,
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.connect_timeout,
            pool=settings.connect_timeout,
        ),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=32),
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
    flight.cancel_all()
    await app.state.client.aclose()
    await app.state.anthropic_client.aclose()


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


def _source_hash() -> str:
    """A hash of the code this process runs: the graph script and skills as graph.py loaded
    them, and the bridge's own modules. Measured (d13): the bridge started at 19:25, dynamic.js
    was edited at 22:20, and the round's code was no commit - a score must name what ran."""
    import sys
    digest = hashlib.sha256()
    for text in (graph.SCRIPT, graph.DYNAMIC_SCRIPT, *(graph.SKILLS[k] for k in sorted(graph.SKILLS))):
        digest.update(text.encode())
    for name in sorted(n for n in sys.modules if n.startswith(__package__ or "agentaus_bridge")):
        path = getattr(sys.modules[name], "__file__", None)
        if path and path.endswith(".py"):
            try:
                with open(path, "rb") as fh:
                    digest.update(fh.read())
            except OSError:
                pass
    return digest.hexdigest()[:16]


_SOURCE_HASH: str | None = None


@app.get("/healthz/upstream")
async def healthz_upstream() -> JSONResponse:
    """Agentaus as the bridge has seen it over the last ten minutes: outcomes (request-specific
    failures - time-up turns, retries of a failed request - left out), the adaptive limit,
    whether it is down and for how long in all, and the hash of the code this process loaded.
    For the harness: a round launched into a slump, or scored on a bridge running other code,
    says nothing about the graph."""
    global _SOURCE_HASH
    if _SOURCE_HASH is None:
        _SOURCE_HASH = _source_hash()
    gate = throttle.limit()
    return JSONResponse({**throttle.health().snapshot(gate.limit, gate.ceiling, gate.busy),
                         "flights": flight.running(), "source_hash": _SOURCE_HASH})


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

def _route(request: Request, body: dict, model: str, *, main_loop: bool) -> tuple[bool, bool]:
    """(send to Agentaus, because it is a subagent following its session).

    By model id, as always - plus a subagent goes wherever its session's main loop last
    went. See `agentaus_bridge.sessions` for why the model id alone sent Agentaus
    sessions' subagents to Claude. Only a main-loop turn (tools offered, no agent id)
    records the session's model: a title or summary call must not flip it.
    """
    by_model = settings.routes_to_agentaus(model) or not settings.passthrough_enabled
    if not settings.agentaus_subagents_follow_session:
        return by_model, False
    session = sessions.session_of(request.headers)
    if sessions.is_subagent(request.headers, body):
        if not by_model and sessions.main_loop_on_agentaus(session):
            return True, True
        return by_model, False
    if main_loop and body.get("tools"):
        sessions.note_main_loop(session, by_model)
        if by_model:
            sessions.note_request(session, _last_user_text(body))
    return by_model, False


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
    to_agentaus, inherited = _route(request, body, model, main_loop=True)

    _request_id.set(_new_request_id())
    messages_in = len(body.get("messages") or [])
    rlog(
        logging.INFO,
        "recv model=%s route=%s stream=%s msgs=%d est=%d bytes=%d",
        model or "(none)",
        ("agentaus (subagent of an Agentaus session)" if inherited
         else "agentaus" if to_agentaus else "anthropic"),
        wants_stream,
        messages_in,
        estimate_request_tokens(body),
        len(raw),
    )

    if settings.log_bodies:
        rlog(logging.INFO, "request body: %s", json.dumps(body)[: settings.log_body_chars])

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
    if _route(request, body, model, main_loop=False)[0]:
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
# timed out) is common on a long summarisation request. 402 is a spent quota: nothing
# succeeds until it is topped up, so it is waited out like an outage - the throttle marks
# the upstream down and the backoff tries a few times an hour. Measured (d16): passed
# through, a 402 ended two runs' sessions mid-graph with "API Error: 402".
_RETRYABLE_STATUS = {402, 408, 425, 429, 500, 502, 503, 504,
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
    """Exponential backoff with jitter, capped so a long outage is retried a few times an hour.

    Jitter matters when several requests fail at once - Claude Code fires background
    calls alongside the main turn, and without it they would all retry in lockstep and
    re-hammer an upstream that is already struggling.
    """
    base = settings.retry_backoff_seconds * (2 ** min(attempt, 30))
    return min(base, settings.retry_max_delay_seconds) + random.uniform(
        0, settings.retry_backoff_seconds
    )


# A failed gateway attempt usually took Agentaus's ~125 s timeout before it failed.
_FAILED_ATTEMPT_S = 125.0


async def _sleep_before_retry(attempt: int, why: str) -> None:
    delay = _retry_delay(attempt)
    rlog(logging.WARNING, "upstream attempt %d failed (%s); retrying in %.2fs", attempt + 1, why, delay)
    # Neither the failed attempt nor this wait counts against the turn's timeout: retrying
    # through a slump is waiting, not being wedged (see flight.Flight.served).
    flight.note_wait(delay + (_FAILED_ATTEMPT_S if why.startswith(("HTTP 52", "HTTP 504", "ReadTimeout")) else 0.0))
    await asyncio.sleep(delay)


# Gateway timeouts are in _RETRYABLE_STATUS because a busy gateway often succeeds on a
# second try. That is true of a small request and false of a large one: if the origin
# could not finish this prompt in time, it will not finish the identical prompt in time
# either. Replaying it costs the timeout again and changes nothing.
#
# Observed: a zoom condensation 524'd and was replayed four times, roughly two minutes
# apart - eight minutes spent to arrive back where it started, while the caller had a
# perfectly good fallback it could have used immediately.
_CAPACITY_STATUS = {504, 520, 522, 524}


@contextlib.asynccontextmanager
async def _watching_for_stall(label: str):
    """Log, at intervals, that an upstream call has not come back yet.

    Without this a stall is indistinguishable from silence. A request was observed
    received, never forwarded upstream, and never answered - and the only evidence was
    that the log had stopped, which is not evidence anyone finds while it is happening.
    The call is not interrupted: reporting a stall and causing one are different jobs,
    and the timeout already owns the second.
    """
    interval = settings.stall_warning_seconds
    if interval <= 0:                                   # 0 disables, as elsewhere here
        yield
        return

    started = time.monotonic()
    # The transport notes here when the call got its slot (see throttle.WAIT_STATE). Measured
    # (d14): "attempt 3 still waiting after 7380s - upstream has not responded" was time in the
    # bridge's own queue - the read timeout is 300 s - and read as an upstream outage.
    state: dict = {"queued_at": None, "sent_at": None, "queued_s": 0.0}
    throttle.WAIT_STATE.set(state)

    async def announce():
        # The task inherits the current context, so the request id is the one that
        # started the call rather than whatever is current when the warning fires.
        waited = 0.0
        while True:
            await asyncio.sleep(interval)
            waited += interval
            if state["queued_at"] is not None and state["sent_at"] is None:
                gate = throttle.limit()
                rlog(logging.WARNING,
                     "%s still queued after %.1fs - waiting for an upstream slot (%d busy, "
                     "limit %d); not sent yet", label, waited, gate.busy, gate.limit)
            else:
                sent = state["sent_at"] or started
                rlog(logging.WARNING,
                     "%s still waiting after %.1fs - upstream has not responded in %.1fs "
                     "(%.1fs queued first)", label, waited,
                     time.monotonic() - sent, state["queued_s"])

    watcher = asyncio.ensure_future(announce())
    try:
        yield
    finally:
        watcher.cancel()
        elapsed = time.monotonic() - started
        if elapsed > interval:
            rlog(logging.INFO, "%s completed after %.1fs (%.1fs of it queued)", label, elapsed,
                 state["queued_s"])


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_body: dict,
    headers: dict,
    retry_capacity: bool = True,
    stream_upstream: bool = False,
    kind: str = "normal",
    give_up: Callable[[], bool] | None = None,
) -> httpx.Response:
    """POST, retrying transient connection faults and gateway status codes.

    Only used for buffered (non-streaming) requests, where replaying costs nothing
    because no bytes have been handed to the client yet.

    `retry_capacity=False` surfaces a gateway timeout immediately instead of replaying
    it. Callers that can make the request smaller - split a chunk, truncate a passage,
    recompact a conversation - want the failure now, not four timeouts later.

    `stream_upstream` asks Agentaus to stream and assembles the reply into the response a
    buffered call returns (see _post_streamed). `kind` is what the request is to the throttle
    (see throttle.REQUEST_KIND); after a gateway failure every retry is a "retry". `give_up`
    is asked before each replay of a gateway failure: True returns the failure instead -
    a graph agent past its limit, whose turn the caller will answer itself.
    """
    last_exc: Exception | None = None
    failed_before = False
    for attempt in range(settings.max_retries + 1):
        throttle.REQUEST_KIND.set(kind if not failed_before or kind != "normal" else "retry")
        try:
            async with _watching_for_stall(f"upstream POST (attempt {attempt + 1})"):
                if stream_upstream:
                    response = await _post_streamed(client, url, json_body, headers)
                else:
                    response = await client.post(url, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            last_exc = exc
            failed_before = True
            if (not _is_retryable_exception(exc) or attempt == settings.max_retries
                    or (give_up is not None and give_up())):
                raise
            await _sleep_before_retry(attempt, _describe(exc))
            continue
        finally:
            throttle.REQUEST_KIND.set("normal")

        if not retry_capacity and response.status_code in _CAPACITY_STATUS:
            await response.aread()
            rlog(logging.WARNING,
                 "HTTP %d on a helper call; surfacing it rather than replaying the same "
                 "prompt", response.status_code)
            return response

        if response.status_code in _RETRYABLE_STATUS and attempt < settings.max_retries:
            failed_before = True
            if give_up is not None and give_up():
                await response.aread()
                rlog(logging.WARNING, "HTTP %d on a graph agent past its limit; not replaying "
                     "it", response.status_code)
                return response
            await response.aread()  # release the connection before sleeping
            await _sleep_before_retry(attempt, f"HTTP {response.status_code}")
            continue
        if response.status_code == 200 and attempt < settings.max_retries:
            # An HTTP 200 carrying an error object: retry it when it is the upstream's own
            # transport failing rather than anything about this request.
            try:
                in_band = response.json().get("error")
            except (ValueError, AttributeError):
                in_band = None
            if isinstance(in_band, dict) and _is_transient(str(in_band.get("message") or "")):
                await _sleep_before_retry(
                    attempt, f"in-band: {str(in_band.get('message'))[:60]}")
                continue
        return response

    raise last_exc if last_exc else RuntimeError("retry loop exited without a response")


async def _completion_chunks(upstream: httpx.Response) -> AsyncIterator[dict]:
    """The chunks of an Agentaus reply: its SSE `data:` objects, or - when it answered a
    stream request with one JSON body anyway - that body as a single chunk in delta form."""
    content_type = (upstream.headers.get("content-type") or "").lower()
    if "event-stream" not in content_type:
        raw = await upstream.aread()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("error"), dict) or "choices" not in data:
            yield data
            return
        choices = []
        for i, choice in enumerate(data.get("choices") or []):
            message = (choice or {}).get("message") or {}
            delta = {"content": message.get("content")} if message.get("content") else {}
            if message.get("tool_calls"):
                delta["tool_calls"] = [{"index": k, **call}
                                       for k, call in enumerate(message["tool_calls"])]
            choices.append({"index": choice.get("index", i), "delta": delta,
                            "finish_reason": choice.get("finish_reason")})
        yield {"choices": choices, **({"usage": data["usage"]} if data.get("usage") else {})}
        return
    async for line in upstream.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            return
        try:
            chunk = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(chunk, dict):
            yield chunk


async def _post_streamed(client: httpx.AsyncClient, url: str, json_body: dict,
                         headers: dict) -> httpx.Response:
    """One Agentaus call made as a stream and returned as the buffered response it replaces:
    the status and body of a failure, or the chunks assembled into one completion. Measured
    (d13, d14 slumps): buffered calls drew 524 about 76 times in 109, streamed ones 7 in 38."""
    async with client.stream("POST", url, json={**json_body, "stream": True},
                             headers=headers) as upstream:
        if upstream.status_code >= 400:
            raw = await upstream.aread()
            return httpx.Response(upstream.status_code, content=raw,
                                  headers={"content-type": upstream.headers.get(
                                      "content-type", "text/plain")},
                                  request=upstream.request)
        text: list = []
        accumulator = ToolCallAccumulator()
        finish, usage = None, None
        async for chunk in _completion_chunks(upstream):
            if isinstance(chunk.get("error"), dict) or "choices" not in chunk:
                return httpx.Response(200, json=chunk, request=upstream.request)
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                if delta.get("tool_calls"):
                    accumulator.add(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
        message: dict = {"role": "assistant", "content": "".join(text) or None}
        calls = accumulator.drain()
        if calls:
            message["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"],
                                                   "arguments": c["arguments"] or "{}"}}
                                     for c in calls]
        assembled = {"choices": [{"index": 0, "message": message, "finish_reason": finish}]}
        if usage:
            assembled["usage"] = usage
        return httpx.Response(200, json=assembled, request=upstream.request)


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
_MAX_MODEL_LEN = re.compile(r"max_model_len\s+(\d+)|maximum context length is\s+(\d+)")
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
    value = int(match.group(1) or match.group(2))
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


HELPER_SYSTEM = (
    "You are a precise assistant working inside a software engineering tool. Do exactly "
    "what the message asks, in exactly the output format it asks for. No preamble, no "
    "commentary, no questions back."
)


async def _agentaus_summarise(
    client: httpx.AsyncClient, text: str, *, on_piece=None, stream: bool | None = None
) -> str:
    """Ask Agentaus to compact a slice of the conversation.

    Deliberately delegated to the model rather than to heuristics here: what is worth
    keeping out of an engineering conversation is a judgement call, and the model is
    better placed to make it than any rule the bridge could hard-code.
    """
    payload = {
        # A system message is what `system_prompt_overwrite` overwrites WITH. Sent without
        # one, Agentaus keeps its own ~2,200-token general-assistant persona: measured,
        # "Say hi" cost 2,442 input tokens bare and 207 with a one-line system message,
        # and with tools offered the bare form refused to use them. Every helper call -
        # plan, review, search chunk, compaction - paid that and carried the persona.
        "messages": [{"role": "system", "content": HELPER_SYSTEM},
                     {"role": "user", "content": text}],
        "stream": settings.agentaus_stream_helpers if stream is None else stream,
        "system_prompt_overwrite": True,
    }
    started_at = time.monotonic()

    limit = settings.agentaus_helper_timeout_seconds
    if limit > 0:
        try:
            return await asyncio.wait_for(
                _helper_call(client, payload, text, started_at, on_piece), timeout=limit
            )
        except asyncio.TimeoutError:
            rlog(logging.WARNING, "helper call exceeded %.0fs and was abandoned", limit)
            raise RuntimeError(f"helper call timed out after {limit:.0f}s")
    return await _helper_call(client, payload, text, started_at, on_piece)


async def _helper_call(
    client: httpx.AsyncClient, payload: dict, text: str, started_at: float, on_piece=None
) -> str:
    """One helper call, streamed or buffered. Bounded by the caller."""
    if payload["stream"]:
        # Streamed rather than buffered. A buffered helper call holds a connection open
        # while the server composes the entire reply, and a fan-out of those is what
        # saturates Agentaus - a batch run once queued ~135 of them and stopped making
        # progress at all. Streaming returns the first bytes as they are generated, so a
        # slow reply stops looking like a dead one and the connection drains sooner.
        try:
            return await _stream_helper(client, payload, text, started_at, on_piece)
        except (httpx.HTTPError, RuntimeError) as exc:
            # Falling back rather than failing: a streaming fault must not cost the
            # caller its summary when the buffered path still works.
            rlog(logging.WARNING, "streamed helper call failed (%s); retrying buffered", exc)
            payload = {**payload, "stream": False}

    response = await _post_with_retry(
        client, settings.agentaus_url, json_body=payload, headers=_agentaus_headers(),
        retry_capacity=False,
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


async def _stream_helper(
    client: httpx.AsyncClient, payload: dict, text: str, started_at: float, on_piece=None
) -> str:
    """Consume a streamed helper reply and return the assembled content."""
    parts: list[str] = []
    first_byte = 0.0
    async with client.stream(
        "POST", settings.agentaus_url, json=payload, headers=_agentaus_headers()
    ) as upstream:
        if upstream.status_code >= 400:
            detail = (await upstream.aread()).decode("utf-8", "replace")[:300]
            _learn_limit_from(detail)
            raise RuntimeError(f"HTTP {upstream.status_code}: {detail}")
        async for chunk in _completion_chunks(upstream):
            # Agentaus reports some failures as HTTP 200 with an error object inside the
            # stream; without this the reply looks empty and successful.
            if isinstance(chunk.get("error"), dict):
                message = str(chunk["error"].get("message") or "")
                _learn_limit_from(message)
                raise RuntimeError(message[:200] or "error inside the helper stream")
            for choice in chunk.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    if not parts:
                        first_byte = time.monotonic() - started_at
                    parts.append(piece)
                    if on_piece is not None:
                        on_piece(piece)
    rlog(logging.DEBUG, "helper stream ok in %.1fs (first byte %.1fs, %d chars in)",
         time.monotonic() - started_at, first_byte, len(text))
    return "".join(parts)


async def _answer_without_tools(client: httpx.AsyncClient, payload: dict) -> str:
    """Ask for an answer from what is already in the conversation, tools removed.

    Used where the model has the material and will not settle: it has searched its budget
    away, or it returned nothing at all. Taking the tools off the request removes the
    option it keeps choosing, and everything it found is still in the messages.

    Returns "" on any failure, so the caller can fall back to saying what happened.
    """
    stripped = {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")}
    messages = list(stripped.get("messages") or [])
    messages.append({
        "role": "user",
        "content": prompt_style.section(
            "task",
            "Answer now, from what is already above. You have no tools for this "
            "turn.\n\nIf what you gathered is enough, give the answer in the format "
            "originally asked for. If it genuinely is not, say in one line what is "
            "missing - do not describe your search.",
        ),
    })
    stripped["messages"] = messages
    stripped["stream"] = False
    try:
        async with hold("forced answer", "urgent"):
            response = await _post_with_retry(
                client, settings.agentaus_url,
                json_body=stripped, headers=_agentaus_headers(),
                retry_capacity=False,
            )
        if response.status_code >= 400:
            return ""
        data = response.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if text.strip():
            rlog(logging.INFO, "forced answer produced %d chars", len(text))
        return text.strip()
    except Exception as exc:
        rlog(logging.WARNING, "forced answer failed (%s)", exc)
        return ""


# What a rewrite pass is shown of an answer. A pass shown only part of one must not
# replace all of it: the final synthesis of a workflow's results runs past this, and a
# revision written from its first 12,000 characters silently dropped the rest.
_REWRITE_CHARS = 12000


def _too_long_to_rewrite(answer: str, what: str) -> bool:
    if len(answer or "") <= _REWRITE_CHARS:
        return False
    rlog(logging.INFO, "%s skipped: the answer is %d chars, past the %d a rewrite sees",
         what, len(answer), _REWRITE_CHARS)
    return True


async def _fix_syntax(client: httpx.AsyncClient, answer: str) -> str:
    """If the answer's Python does not parse, ask once for the typo to be fixed.

    Deterministic detection, so this costs nothing on an answer that is fine. Any failure
    returns the original: a broken repair must not lose a mostly-correct answer.
    """
    error = syntax.first_error(answer)
    if not error or _too_long_to_rewrite(answer, "syntax fix"):
        return answer
    rlog(logging.WARNING, "answer does not parse (%s); asking for a fix", error)
    try:
        async with hold("syntax fix", "urgent"):
            fixed = await _agentaus_summarise(
                client, syntax.FIX_INSTRUCTION.format(error=error, answer=answer[:12000])
            )
    except Exception as exc:
        rlog(logging.WARNING, "syntax fix failed (%s); keeping the answer", exc)
        return answer
    if not fixed or not fixed.strip():
        return answer
    if syntax.first_error(fixed):
        # Still broken. The original is no worse and may be closer to right.
        rlog(logging.WARNING, "the fix does not parse either; keeping the original")
        return answer
    rlog(logging.INFO, "syntax fixed (%d -> %d chars)", len(answer), len(fixed))
    return fixed.strip()


def _grounding_applies(body: dict, answer: str, ran: dict | None) -> bool:
    """Whether to check this answer against the evidence rather than self-review it.

    A turn whose facts came from the bridge's own tools is a tool turn, even though no
    client tool appears in the message list. Treated as a prose turn, it went to the
    self-review, which sees only the request and the answer. Observed: an answer citing
    `src/retry.py` lines 7-9, just read by `agentaus_zoom`, was "revised" into a
    description of urllib3's `Retry.get_backoff_time()` - from memory, in a project that
    does not contain urllib3.
    """
    # A subagent's answer is the value its parent reads; a rewrite would replace it.
    # And a reply that only reports a background launch makes no claim to check: graded
    # as one, "the workflow is running" became "the repository may contain a workflow;
    # I have not examined it".
    if is_subagent_turn(body) or awaiting_background(body):
        return False
    minimum = settings.agentaus_review_min_chars
    if ran:
        return len((answer or "").strip()) >= minimum
    return worth_grounding_check(body, answer, min_chars=minimum)


def _bridge_evidence(ran: dict | None, *, result_chars: int = 2500,
                     budget: int = 24000) -> str:
    """What the bridge's own tools returned this turn, in the evidence ledger's shape.

    The ledger is built from the message list Claude Code sent, and bridge tool calls
    never enter it - they run inside the turn. So a fact found by `agentaus_search` or
    read with `agentaus_zoom` looked unsupported. Observed: an answer stating
    `CACHE_TTL_SECONDS = 600` in `src/config.py`, read by `agentaus_zoom` a minute
    earlier, was judged to "need a tool read of src/config.py" and rewritten to "I have
    not read it" - a correct, evidenced answer destroyed by the pass meant to protect it.
    """
    if not ran:
        return ""
    blocks: list[str] = []
    spent = 0
    for signature, result in reversed(list(ran.items())):
        try:
            key = json.loads(signature)
        except ValueError:
            key = {}
        name = key.get("n") or "bridge tool"
        args = json.dumps(key.get("a") or {}, default=str, ensure_ascii=False)[:200]
        room = min(result_chars, max(0, budget - spent))
        body = ledger.excerpt(result, room) if room and (result or "").strip() else ""
        spent += len(body)
        head = f"- {name}({args}) -> ok"
        blocks.append(f"{head}\n  <output>\n{body}\n  </output>" if body else head)
    blocks.reverse()
    return (
        f"\n[{len(ran)} bridge tool call(s) ran inside this turn on the agent's behalf - "
        "searches and file reads. Each is followed by what it returned. A value that "
        "appears in an <output> below IS supported: the agent read it there.]\n"
        + "\n".join(blocks)
    )


async def _check_grounding(
    client: httpx.AsyncClient, body: dict, answer: str, bridge_ran: dict | None = None
) -> str:
    """Check an answer against what the turn actually ran, and strip what it cannot support.

    This is the pass ordinary self-review cannot be: the reviewer is given the ledger of
    tools actually executed, so "you claim this about a file you never opened" becomes a
    checkable statement rather than a guess.

    It exists because the ordinary review has to sit out these turns - a reviewer shown
    only the request and the answer reads a well-grounded reply as an unverified claim -
    and that left the one kind of turn where claims outrun evidence with nothing watching
    it at all. Observed: a survey of a repository asserting what was in files it had never
    read, and citing a policy that does not exist.

    Returns the answer to use. Any failure returns the original: a broken check must never
    cost a good answer.
    """
    ran = "\n".join(part for part in (
        ledger.render_evidence(body.get("messages") or [], limit=60),
        ledger.render_notifications(body.get("messages") or []),
        _bridge_evidence(bridge_ran),
    ) if part)
    if not ran or _too_long_to_rewrite(answer, "grounding check"):
        return answer
    try:
        async with hold("grounding check", "background"):
            reply = await _agentaus_summarise(
                client,
                GROUNDING_INSTRUCTION.format(ledger=ran, answer=answer[:12000]),
            )
        gaps = grounding_verdict(reply)
        if not gaps:
            return answer
        rlog(logging.WARNING, "grounding check found unsupported claims:\n%s", gaps[:600])
        async with hold("grounding rewrite", "background"):
            revised = await _agentaus_summarise(
                client,
                STRIP_UNGROUNDED_INSTRUCTION.format(
                    answer=answer[:12000], gaps=gaps[:4000]
                ),
            )
        if revised and revised.strip():
            rlog(logging.INFO, "stripped ungrounded claims (%d -> %d chars)",
                 len(answer), len(revised))
            return revised.strip()
    except Exception as exc:
        rlog(logging.WARNING, "grounding check failed (%s); keeping the answer", exc)
    return answer


async def _self_review(client: httpx.AsyncClient, request_text: str, answer: str) -> str:
    """Have Agentaus critique its own answer and revise it if defects are found.

    Returns the answer to use - the original when the review is clean, or a revision.
    Any failure returns the original: a broken review must never lose a good answer.
    """
    if _too_long_to_rewrite(answer, "self-review"):
        return answer
    try:
        async with hold("self-review", "background"):
            review = await _agentaus_summarise(
                client,
                REVIEW_INSTRUCTION.format(
                    request=request_text[:12000], answer=answer[:12000]
                ),
            )
        verdict = declared_verdict(review)
        if verdict is None:
            # The reviewer did not follow the format. Ask it rather than guessing from
            # the prose: sniffing for "OK" misreads both "OK, but the empty case is
            # broken" and a bare approval wrapped in markdown, and the two mistakes
            # fail in opposite directions.
            async with hold("review adjudication", "background"):
                adjudication = await _agentaus_summarise(
                    client, ADJUDICATE_INSTRUCTION.format(review=review[:6000])
                )
            verdict = not adjudication.strip().upper().startswith("YES")
            rlog(logging.INFO, "review verdict was unstated; adjudicated as %s",
                     "sound" if verdict else "defective")
        if verdict:
            return answer
        async with hold("revision", "background"):
            revised = await _agentaus_summarise(
                client,
                REVISE_INSTRUCTION.format(
                    request=request_text[:12000], answer=answer[:12000],
                    defects=review[:6000],
                ),
            )
        if revised and revised.strip():
            rlog(logging.INFO, "self-review revised the answer (%d -> %d chars)", len(answer), len(revised))
            return revised.strip()
    except Exception as exc:
        rlog(logging.WARNING, "self-review failed (%s); keeping the original answer", exc)
    return answer


async def _plan_turn(client: httpx.AsyncClient, body: dict, on_piece=None) -> str:
    """Ask Agentaus to plan this turn before it answers it.

    Agentaus has no native thinking mode, so left alone it answers from the first thing
    that comes to mind - the documented "started editing before working out what the
    change required" failure. Asking for the plan as its own turn is the same trade the
    review pass makes: two cheap passes beat one on a smaller model.

    Never raises. A failed plan returns "" and the turn proceeds unplanned, exactly as
    it does today - a broken planner must not cost the user their answer.
    """
    try:
        async with hold("planning", "urgent"):
            # Streamed when someone is watching it form, so the plan appears as it is
            # written rather than all at once after five to fifteen silent seconds.
            plan = await _agentaus_summarise(
                client, plan_prompt(_last_user_text(body), body,
                                    routing=settings.agentaus_skill_routing),
                on_piece=on_piece, stream=True if on_piece is not None else None,
            )
    except Exception as exc:
        rlog(logging.WARNING, "planning pass failed (%s); answering unplanned", exc)
        return ""
    plan = (plan or "").strip()
    if plan:
        rlog(logging.INFO, "planned the turn in %d chars", len(plan))
    return plan


def _openai_calls(data: dict) -> list:
    """Tool calls from a non-streaming Agentaus response, in the accumulator's shape."""
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    return [
        {
            "id": call.get("id") or "",
            "name": (call.get("function") or {}).get("name") or "",
            "arguments": (call.get("function") or {}).get("arguments") or "{}",
        }
        for call in (message.get("tool_calls") or [])
    ]


def _recover_harmony_calls(data: dict) -> tuple:
    """Pull tool calls the model wrote into its answer text back out of it.

    Returns the response with that markup removed, plus the calls it contained. A call
    written as prose is still a call the model meant to make, and the alternative is
    showing the user a line of angle brackets and calling it an answer.
    """
    choices = list(data.get("choices") or [])
    if not choices:
        return data, []
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    text = message.get("content") or ""
    if not harmony.looks_like_harmony(text):
        return data, []

    cleaned, calls = harmony.extract(text)
    message["content"] = cleaned or None
    choice["message"] = message
    return {**data, "choices": [choice] + choices[1:]}, calls


def _with_forced_answer(data: dict, text: str) -> dict:
    """Put a forced answer into a response, clearing the tool calls it replaces."""
    choices = list(data.get("choices") or [{}])
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    message["content"] = text
    message["tool_calls"] = None
    choice["message"] = message
    choice["finish_reason"] = "stop"
    return {**data, "choices": [choice] + choices[1:]}


def _without_bridge_calls(data: dict) -> dict:
    """Strip bridge-owned tool calls from a response.

    Needed when the tool-round budget runs out with one still pending: Claude Code has
    never heard of `agentaus_search` and would fail the tool_use rather than run it, so
    an unanswered bridge call must never be surfaced.
    """
    choices = list(data.get("choices") or [])
    if not choices:
        return data
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    kept = [
        call for call in (message.get("tool_calls") or [])
        if ((call.get("function") or {}).get("name") or "") not in bridge_tools.BRIDGE_TOOLS
    ]
    message["tool_calls"] = kept or None
    if not kept and not (message.get("content") or "").strip():
        message["content"] = (
            "I ran out of search rounds before finishing. Ask again and I will continue "
            "from what I found."
        )
    choice["message"] = message
    if not kept and choice.get("finish_reason") == "tool_calls":
        choice["finish_reason"] = "stop"
    return {**data, "choices": [choice] + choices[1:]}


def _known_tool_names(payload: dict) -> set:
    """Every tool name the model was actually offered, from the payload it was sent."""
    names = set()
    for tool in (payload or {}).get("tools") or []:
        name = ((tool or {}).get("function") or {}).get("name")
        if name:
            names.add(name)
    return names


def _canonical(name: str, known: set) -> str | None:
    """The offered tool `name` refers to, allowing for how models mangle names.

    `read` for `Read`, `agentaus-search` for `agentaus_search`. Observed live: a turn
    spent one of its three tool rounds being corrected from `read` to `Read`, which is
    not a mistake worth a round trip - it is the same tool, spelled carelessly.
    """
    if name in known:
        return name
    squashed = name.lower().replace("_", "").replace("-", "").replace(" ", "")
    for candidate in known:
        if candidate.lower().replace("_", "").replace("-", "").replace(" ", "") == squashed:
            return candidate
    return None


def _partition_tool_calls(
    calls: list, known: set | None = None, library: frozenset = frozenset()
) -> tuple[list, list, list]:
    """Split tool calls into (bridge-owned, client-owned, invented).

    The third bucket is not defensive programming - it is a failure observed on the
    first live Agentaus turn, which answered a search by calling a tool named
    `open_file` that no one had offered it. Passing that through means Claude Code
    fails a tool_use for a tool it has never heard of, and the turn dies on an error
    that looks like the bridge's fault.

    `known` is the set of names actually sent upstream. Without it nothing can be
    called invented, so the check is skipped rather than guessed at.
    """
    mine, theirs, invented = [], [], []
    for call in calls:
        name = call.get("name") or ""
        if known:
            pool = known | set(bridge_tools.BRIDGE_TOOLS)
            resolved = _canonical(name, pool) or repair.resolve(name, pool)
            if resolved is None:
                invented.append(call)
                continue
            if resolved != name:
                rlog(logging.INFO, "tool name %r resolved to %r", name, resolved)
                call = {**call, "name": resolved, "asked_as": name}
                name = resolved
        if name in bridge_tools.BRIDGE_TOOLS or _is_library_skill_call(name, call, library):
            mine.append(call)
        else:
            theirs.append(call)
    return mine, theirs, invented


def _is_library_skill_call(name: str, call: dict, library: frozenset) -> bool:
    """A `Skill` call naming one of the library skills this request's listing was given.

    Those are the bridge's to answer: Claude Code has never heard of them and would fail
    the call. `library` is passed in, not read from a context variable - the streaming
    path runs `prepare` as its own task, so a variable it set never reached this check.
    """
    if name != "Skill" or not library:
        return False
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return False
    skill = str((arguments or {}).get("skill") or "").strip()
    return bool(skill) and skill in library


def _correction_for(call: dict, known: set) -> str:
    """What to tell a model that called a tool which does not exist."""
    offered = ", ".join(sorted(known)) or "(none)"
    return (
        f"There is no tool named {call.get('name')!r}. You invented it. "
        f"The tools you actually have are: {offered}. "
        f"Call one of those by its exact name, or answer from what you already know. "
        f"Do not guess at a tool name again."
    )


def _tool_schemas(payload: dict) -> dict:
    """Each offered tool's input schema, by name - the client's own and the bridge's.

    Both live in the same payload: bridge tools are injected into `tools` alongside the
    client's before the request goes upstream, so one lookup validates either kind.
    """
    schemas = {}
    for tool in (payload or {}).get("tools") or []:
        function = (tool or {}).get("function") or {}
        name = function.get("name")
        if name:
            schemas[name] = function.get("parameters") or {}
    return schemas


def _graph_main_loop(body: dict) -> bool:
    """A main loop whose `Workflow` call the bridge runs its graph in place of."""
    return bool(settings.agentaus_workflow_graph and not is_subagent_turn(body)
                and workflow_opted_in(body))


def _writes_plain_text(body: dict) -> bool:
    """Whether this request's text writes are made plain - see graph.plain_text_writes. A
    graph agent's, and an opted-in main loop's; a plain user session is left as it is."""
    return (graph.is_graph_task(body) and is_subagent_turn(body)) or _graph_main_loop(body)


def _graph_session(body: dict) -> dict | None:
    """This session's graph record (see graph.note_run), from the body as the client sent it."""
    entry = graph.note_run(body)
    if entry is not None and entry.get("said") is None:
        # Started from a launch the bridge had no record of: what the user had said by then.
        cut = entry.get("found_at") or 0
        entry["said"] = _last_user_text({"messages": (body.get("messages") or [])[:cut]})
    return entry


GRAPH_ALREADY_RAN = (
    "Your `Workflow` call was not run: the agent graph already ran in this session and "
    "reported. Make the fix its result names yourself: `Read` the lines, copy `old_string` "
    "from that Read exactly (indentation included), and `Edit`."
)
GRAPH_STILL_RUNNING = (
    "Your `Workflow` call was not run: the agent graph is already running in this session "
    "({tasks}). Do not start another. Wait for its result - it arrives in a message "
    "containing `<task-notification>` - or end your turn saying what is running."
)


def _graph_reruns(calls: list, body: dict) -> list:
    """(call, correction) for a second `Workflow` call in a session whose graph has run.

    Measured (d12 he10, c1913h): a second graph, started after the first had reported, ran on
    a 0-char task until the 7,200 s kill, and its picks would have taken he10 from 0.8 to 0.6.
    A new request from the user since the graph started may start a new one; while one is
    running, nothing may. Pass the body as the client sent it.
    """
    if not _graph_main_loop(body):
        return []
    workflows = [c for c in calls if c.get("name") == "Workflow"]
    if not workflows:
        return []
    entry = _graph_session(body)
    state = graph.run_state(entry)
    if state == "running":
        tasks = ", ".join(sorted(t for t in entry["launched"] - entry["reported"] if t != "?"))
        return [(c, GRAPH_STILL_RUNNING.format(tasks=tasks or "launched earlier")) for c in workflows]
    if state == "reported" and not _new_request_since(body, entry):
        return [(c, GRAPH_ALREADY_RAN) for c in workflows]
    return []


def _new_request_since(body: dict, entry: dict) -> bool:
    """Whether the user has asked for something since the graph started. Nothing said (the
    request compacted away) and Claude Code's own compaction summary are not a new request."""
    said = _last_user_text(body)
    return bool(said) and said != (entry.get("said") or "") and not said.startswith(
        "This session is being continued from a previous conversation")


def _graph_not_started(body: dict) -> bool:
    """A graph main loop before its first Workflow call - the body as the client sent it."""
    if not _graph_main_loop(body) or graph.run_state(graph.note_run(body)) != "none":
        return False
    return not any(
        isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Workflow"
        for m in body.get("messages") or [] if m.get("role") == "assistant"
        and isinstance(m.get("content"), list) for b in m["content"])


def _without_graph_script(body: dict) -> tuple[dict, int]:
    """`body` with each Workflow call that carries the bridge's graph cut to its description
    and a stand-in script. Measured (d12 c1913h): one such call held 115,148 characters of
    script, skills and inventory, and on the first request after the graph reported it alone
    pushed the conversation into compaction - which summarised the call away."""
    swapped = 0
    messages = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            messages.append(message)
            continue
        blocks = []
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") == "Workflow" and isinstance(block.get("input"), dict)
                    and graph.is_graph_script(block["input"].get("script"))):
                swapped += 1
                block = {**block, "input": {"description": block["input"].get("description") or "",
                                            "script": graph.SCRIPT_STAND_IN}}
            blocks.append(block)
        messages.append({**message, "content": blocks})
    return ({**body, "messages": messages}, swapped) if swapped else (body, 0)


def _run_graph_instead(calls: list, body: dict) -> None:
    """Put the bridge's agent graph in place of a script Agentaus wrote. See graph.py.

    Pass the body as the client sent it: the task is the user's request, and compaction can
    summarise that away."""
    if not _graph_main_loop(body):
        return
    # The graph sees the whole request: on a follow-up turn, the opening request and the
    # latest message together, not only "now do the next file".
    first, last = _original_request(body), _last_user_text(body)
    task = first if not last or last == first else f"{first}\n\nFollow-up:\n{last}"
    if not task.strip():
        # Measured (d12 he10, c1913h): "running the agent graph for a 0-char request" - its
        # planners invented the rules the solvers then followed.
        task = (graph.note_run(body) or {}).get("task") or ""
        if not task.strip():
            if any(c.get("name") == "Workflow" for c in calls):
                rlog(logging.WARNING, "workflow: no request to run the agent graph on; "
                     "leaving the call alone")
            return
    if not graph.suits(task, working_directory_of(body)):
        return
    said = "\n".join(_user_texts(body))
    for call in calls:
        if call.get("name") != "Workflow":
            continue
        script = graph.script_of(call)
        # A script the user wrote or dictated is theirs to run, not ours to replace.
        if script and len(script) >= 200 and script.strip()[:200] in said:
            rlog(logging.INFO, "workflow: running the user's own script")
            continue
        before = call.get("arguments")
        call["arguments"] = graph.expand(call, task, working_directory_of(body))["arguments"]
        if call["arguments"] != before:
            graph.remember_run(body, task, _last_user_text(body))
            rlog(logging.INFO, "workflow: substituted the agent graph")


def _validate_tool_calls(calls: list, schemas: dict, cwd: str | None = None) -> list:
    """Fix what is unambiguously fixable; report what is not.

    Coerced arguments are written back onto the call, so a double-encoded payload is
    repaired in place rather than spending a correction round. Returns
    `(call, message)` for every call that is still invalid - those must not be passed
    on, because the client will reject them and the turn is spent either way.
    """
    broken = []
    # What the Edits before each one in this response leave in their files: a second Edit to
    # a file is matched against the text after the first, not the disk. See repair.repair.
    pending: dict = {}
    for call in calls:
        schema = schemas.get(call.get("name") or "")
        if not schema:
            continue
        # Unambiguous slips first - a field under another name, a relative path - so
        # they cost nothing rather than a correction round. See repair.py.
        try:
            repaired = repair.repair(
                call, call.get("asked_as") or call.get("name") or "", schema, cwd, pending)
            if repaired.get("correction"):
                broken.append((call, repaired["correction"]))
                continue
            call["arguments"] = repaired["arguments"]
            fixed, problems = tool_schema.validate(call.get("arguments"), schema)
        except Exception as exc:
            # A schema this subset cannot read is the client's to judge. Forwarding the
            # call unchecked costs nothing; raising here ended the whole turn.
            rlog(logging.WARNING, "could not check arguments for %s (%s); forwarding "
                 "unchecked", call.get("name"), exc)
            continue
        if problems:
            broken.append((call, tool_schema.correction_for(
                call.get("name") or "", problems, schema)))
            continue
        encoded = json.dumps(fixed)
        if encoded != call.get("arguments"):
            log.debug("repaired arguments for %s", call.get("name"))
        call["arguments"] = encoded
    return broken


def _guard_imports(calls: list, received: dict, cwd: str | None) -> None:
    """Snapshot the .py files a graph main loop's calls are about to change, so the next request
    can tell whether the change broke importing them. See import_guard.py."""
    if settings.agentaus_import_guard and calls and _graph_main_loop(received):
        if import_guard.remember(calls, cwd):
            rlog(logging.INFO, "import guard: watching this turn's .py edits")


def _with_coerced_calls(data: dict, calls: list) -> dict:
    """Put repaired arguments back into a non-streaming response.

    The streaming path emits from the call dicts themselves, so writing back is enough
    there. This path emits from `data`, which the call dicts were only copied out of.
    """
    # Names too: a call resolved from `read` to `Read` must reach the client as `Read`.
    repaired = {call.get("id"): call for call in calls if call.get("id")}
    if not repaired:
        return data
    choices = list(data.get("choices") or [])
    if not choices:
        return data
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    rebuilt = []
    for call in message.get("tool_calls") or []:
        if call.get("id") in repaired:
            function = dict(call.get("function") or {})
            function["arguments"] = repaired[call["id"]].get("arguments")
            function["name"] = repaired[call["id"]].get("name") or function.get("name")
            call = {**call, "function": function}
        rebuilt.append(call)
    message["tool_calls"] = rebuilt
    choice["message"] = message
    return {**data, "choices": [choice] + choices[1:]}


def _with_tool_results(payload: dict, calls: list, results: list) -> dict:
    """Append calls and their results to a payload, in OpenAI's shape.

    Assistant turn carrying the tool_calls, then one `tool` message per result - the
    same shape the translator produces for the client's own tools, so the model sees no
    difference between a tool the bridge ran and one Claude Code ran.
    """
    messages = list(payload.get("messages") or [])
    messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": call.get("arguments")
                        if isinstance(call.get("arguments"), str)
                        else json.dumps(call.get("arguments") or {}),
                    },
                }
                for call in calls
            ],
        }
    )
    for call, result in zip(calls, results):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id") or "",
                "content": result or "(no output)",
            }
        )
    return {**payload, "messages": messages}


def _without_unknown_calls(data: dict, known: set) -> dict:
    """Strip invented tool calls from a non-streaming response."""
    choices = list(data.get("choices") or [])
    if not choices:
        return data
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    kept = [
        c for c in (message.get("tool_calls") or [])
        if ((c.get("function") or {}).get("name") or "") in known
    ]
    message["tool_calls"] = kept or None
    if not kept and not (message.get("content") or "").strip():
        message["content"] = (
            "I tried to use a tool that does not exist and could not recover. "
            "Ask again and I will use one of the tools I actually have."
        )
    choice["message"] = message
    if not kept and choice.get("finish_reason") == "tool_calls":
        choice["finish_reason"] = "stop"
    return {**data, "choices": [choice] + choices[1:]}


# Free-text fields where a trivial difference in wording is not a different request. A
# semantic search does not care about a trailing bracket; an exact-match signature does,
# and that difference cost a real turn 29 seconds of duplicate fan-out.
_FUZZY_FIELDS = ("query", "question")


# Words that carry no search intent. "Where is the gate" and "the gate, where is it?"
# are one question; the grammar around the nouns is not what is being looked for.
_FUNCTION_WORDS = frozenset("""
a an the is are was were be been am do does did of in on at to for from by with about
this that these those it its what which where when who whom how why and or but if then
i you we they me my our your their there here s
""".split())


def _same_request(text: str) -> str:
    """Reduce a natural-language argument to what actually distinguishes it.

    Observed live: a turn searched `should_think`, then searched `should_think(`, and the
    second ran the whole fan-out again for a result it already had. Those are one
    question, and so are two phrasings of the same sentence.

    Words are lowercased, stripped of punctuation, sorted, and cleared of grammar - a
    search is a bag of intent rather than a sentence. The content words themselves are
    kept, so two genuinely different questions still miss each other, which is the
    direction that matters: a false miss costs a repeated search, a false hit answers a
    question with someone else's result.

    Stripping is skipped only when it would leave nothing at all, so a query that is
    entirely grammar - "what is it" - still distinguishes itself from another one.
    """
    kept = "".join(c if c.isalnum() or c.isspace() else " " for c in (text or "").lower())
    words = kept.split()
    content = [_singular(word) for word in words if word not in _FUNCTION_WORDS]
    return " ".join(sorted(content or words))


def _singular(word: str) -> str:
    """Fold a trailing plural or third-person `s`, so `cap` and `caps` are one word.

    Crude on purpose. It only has to make two phrasings of one question agree, and a
    word it mangles is mangled identically on both sides, so a wrong stem costs nothing.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"                      # retries -> retry
    if len(word) > 4 and word.endswith(("sses", "xes", "zes", "ches", "shes")):
        return word[:-2]                            # classes -> class, boxes -> box
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]                            # caps -> cap
    return word                                     # class, process: not plurals


def _call_signature(call: dict) -> str:
    """A stable key for "this request, already made", for spotting a repeat."""
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": arguments}
    arguments = arguments or {}
    if isinstance(arguments, dict):
        arguments = {
            key: _same_request(value) if key in _FUZZY_FIELDS and isinstance(value, str)
            else value
            for key, value in arguments.items()
        }
    return json.dumps(
        {"n": call.get("name") or "", "a": arguments}, sort_keys=True, default=str
    )


async def _run_bridge_tools(
    client: httpx.AsyncClient,
    payload: dict,
    calls: list,
    default_path: str | None = None,
    seen: dict | None = None,
) -> dict:
    """Execute bridge-owned tool calls and return the payload to send next.

    The results are appended as an assistant turn carrying the tool_calls plus one
    `tool` message per result - OpenAI's shape, matching what the translator produces
    for the client's own tools. Claude Code never sees any of it.

    `seen` remembers what has already run this turn. It has to, because the tool ledger
    cannot: bridge tool calls never enter the Anthropic message list, so the ledger -
    which is derived from that list - is blind to exactly the tools most likely to be
    repeated. Observed live: one turn made twelve bridge calls, zooming the same passage
    twice and re-running a 58-second search, then exhausted its round budget with the
    work unfinished. The model was doing the thing `augment.py` already documents, on the
    only tools nothing was watching.
    """
    seen = seen if seen is not None else {}

    async def one(call: dict) -> str:
        signature = _call_signature(call)
        if signature in seen:
            # Answered from cache, and said so plainly. Repeating the work would cost a
            # round and a minute to hand back bytes the model already has.
            rlog(logging.WARNING, "bridge tool %s repeated with identical arguments; "
                 "returning the earlier result", call.get("name"))
            return (
                "[You already ran this exact call earlier in this turn. This is the same "
                "result, not a new one. Use it and move on - do not call it again.]\n"
                + seen[signature]
            )

        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        started_at = time.monotonic()
        result = await bridge_tools.execute(
            call.get("name") or "", arguments or {},
            lambda text: _agentaus_summarise(client, text),
            default_path,
        )
        rlog(logging.INFO, "bridge tool %s ran in %.1fs -> %d chars",
             call.get("name"), time.monotonic() - started_at, len(result))
        seen[signature] = result
        return result

    results = await asyncio.gather(*[one(call) for call in calls])
    return _with_tool_results(payload, calls, results)


def _head_and_tail(text: str, head: int, tail: int) -> str:
    """The start and end of a long reply - where it says what it is, and how it ends."""
    if len(text) <= head + tail:
        return text
    return text[:head] + f"\n[... {len(text) - head - tail} characters ...]\n" + text[-tail:]


_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _without_reminders(text: str) -> str:
    """The user's words, without Claude Code's <system-reminder> blocks.

    Claude Code puts its own notes - the account email, commit attribution rules, which
    tools have loaded - in the user message beside what the user typed. Handed to the
    planner, reviewer or turn judge as "the request", they were read as part of the task.
    """
    return _REMINDER.sub("", text or "").strip()


def _launches_skill(block) -> bool:
    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
        return False
    text = block.get("content")
    if isinstance(text, list):
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return "Launching skill:" in str(text or "")


def _user_texts(body: dict) -> list:
    """What the user said, message by message: no system reminders, no background-task
    reports, and no skill bodies.

    A skill's body arrives as user text after its `Skill` call ("Launching skill: ...").
    Measured (d12 c1913, he10): the 17.5 KB workflow-authoring reference became the graph's
    "Follow-up:", in every agent's prompt - solver prompts of about 40 k characters.
    """
    out = []
    in_skill = False
    for message in body.get("messages") or []:
        content = message.get("content")
        if message.get("role") == "assistant":
            # Everything the user side sends until the next assistant turn is the skill's.
            in_skill = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill"
                for b in content)
            continue
        if message.get("role") != "user":
            continue
        if isinstance(content, list) and any(_launches_skill(b) for b in content):
            in_skill = True
        if in_skill:
            continue
        if isinstance(content, str):
            text = _without_reminders(content)
        elif isinstance(content, list):
            text = _without_reminders("\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"))
        else:
            text = ""
        if text and "<task-notification>" not in text:
            out.append(text)
    return out


def _last_user_text(body: dict) -> str:
    """The most recent thing the user actually said, as the request a pass judges against."""
    texts = _user_texts(body)
    return texts[-1] if texts else ""


def _original_request(body: dict) -> str:
    """What the conversation was started to do - the first thing the user said."""
    texts = _user_texts(body)
    return texts[0] if texts else ""


# STALLED re-asks per conversation, across requests. The correction budget is per
# request, and Claude Code sends a fresh request after every tool call - so a judge that
# kept calling a finished turn stalled kept it going until the client's turn cap.
# Observed: 76 re-asks in one benchmark run, and a session that had finished its task
# pushed into writing notes into the user's Claude memory folder.
_STALL_REASKS: "OrderedDict[str, tuple[int, int]]" = OrderedDict()
_STALL_REASK_LIMIT = 2
# The budget refills once the model has made this many tool calls since its last re-ask.
# A lifetime budget of two was spent in the first minutes of a long ultracode session,
# and its last reply - "We will apply multiple Edit calls." - ended the run with the bug
# fixes unwritten. Calls made since the re-ask are progress; a model that keeps stopping
# without making any still runs out after two.
_STALL_PROGRESS_CALLS = 3


def _stall_key(body: dict) -> str:
    return hashlib.sha256((_original_request(body) + "\x00"
                           + (working_directory_of(body) or "")).encode()).hexdigest()


def _tool_calls_made(body: dict) -> int:
    return sum(1 for m in body.get("messages") or [] if m.get("role") == "assistant"
               and isinstance(m.get("content"), list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use")


def _may_reask_stalled(body: dict) -> bool:
    key = _stall_key(body)
    calls = _tool_calls_made(body)
    used, at = _STALL_REASKS.get(key, (0, calls))
    if calls - at >= _STALL_PROGRESS_CALLS:
        used = 0
    if used >= _STALL_REASK_LIMIT:
        return False
    _STALL_REASKS[key] = (used + 1, calls)
    _STALL_REASKS.move_to_end(key)
    while len(_STALL_REASKS) > 512:
        _STALL_REASKS.popitem(last=False)
    return True


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
            verify=settings.agentaus_verify_summary,
            block=settings.agentaus_compact_block,
        )
        request.app.state.compactor = existing
    return existing


def _get_distiller(request: Request) -> ResultDistiller:
    """One distiller per process, so its cache survives between turns.

    Without the cache each turn would re-condense the same immutable tool results, which
    is both wasted spend and - worse - a conversation prefix that changes every request,
    which would stop the compactor's cache ever hitting again.
    """
    existing = getattr(request.app.state, "distiller", None)
    if existing is None:
        client = request.app.state.client
        existing = ResultDistiller(
            lambda text: _agentaus_summarise(client, text),
            threshold_tokens=settings.agentaus_distill_threshold_tokens,
            chunk_tokens=settings.agentaus_distill_chunk_tokens,
        )
        request.app.state.distiller = existing
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
        return _clipped_to(fitted, target, reserve)
    if plan["method"] == "trimmed":
        fitted = {**body, "messages": plan["messages"]}
        fitted["system"] = _with_trim_notice(body.get("system"), plan["dropped"])
        rlog(logging.WARNING, 
            "summarisation could not reach the %d-token target; dropped %d oldest "
            "message(s) instead (~%d -> ~%d tokens)",
            target, plan["dropped"], estimated, estimate_request_tokens(fitted),
        )
        return _clipped_to(fitted, target, reserve)
    return _clipped_to(body, target, reserve)


def _tool_result_text(block: dict) -> str | None:
    text = block.get("content")
    if isinstance(text, list):
        if not all(isinstance(b, dict) and b.get("type") == "text" for b in text):
            return None
        return "\n".join(b.get("text", "") for b in text)
    return text if isinstance(text, str) else None


_CLIP_FLOOR_CHARS = 4000


def _clip_largest_tool_results(body: dict, target: int) -> tuple[dict, int]:
    """`body` with its largest tool results cut to head and tail until it fits `target` tokens.

    Measured (d12 c1913): one 400 KB `ls -R` result sat in the unfinished last exchange, which
    compaction never touches ("compaction result: 485857 -> 485857"), and every request after
    it was rejected as too long. Returns (body, how many results were clipped).
    """
    clipped = 0
    for _ in range(8):
        over = estimate_request_tokens(body) - target
        if over <= 0:
            break
        found = None
        for i, message in enumerate(body.get("messages") or []):
            if message.get("role") != "user" or not isinstance(message.get("content"), list):
                continue
            for j, block in enumerate(message["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = _tool_result_text(block)
                    if text and len(text) > _CLIP_FLOOR_CHARS and (
                            found is None or len(text) > len(found[2])):
                        found = (i, j, text)
        if found is None:
            break
        i, j, text = found
        tokens = max(estimate_tokens(text), 1)
        # Cut what is over, and a tenth more for the estimate, never below the floor.
        keep = max(int(len(text) * max(tokens - over, 0) / tokens * 0.9), _CLIP_FLOOR_CHARS)
        if keep >= len(text):
            break
        short = _head_and_tail(text, keep * 2 // 3, keep // 3).replace(
            " characters ...]", " characters cut to fit the context window. Print less: "
            "counts, head, or the lines you need ...]", 1)
        messages = list(body["messages"])
        blocks = list(messages[i]["content"])
        blocks[j] = {**blocks[j], "content": short}
        messages[i] = {**messages[i], "content": blocks}
        body = {**body, "messages": messages}
        clipped += 1
    return body, clipped


def _clipped_to(body: dict, target: int, reserve: int) -> dict:
    """`body`, its largest tool results clipped if it still does not fit after fitting."""
    room = max(target - reserve, target // 2)
    if estimate_request_tokens(body) <= room:
        return body
    clipped_body, clipped = _clip_largest_tool_results(body, room)
    if clipped:
        rlog(logging.WARNING, "still over the window after fitting; clipped %d large tool "
             "result(s) to head and tail (~%d -> ~%d tokens)", clipped,
             estimate_request_tokens(body), estimate_request_tokens(clipped_body))
    return clipped_body


# Failures Agentaus reports INSIDE a 200 response - an error chunk in the stream, or an
# error object in the JSON - that say nothing about the request: its own backend
# dropped. Observed 20 times in one benchmark day as "peer closed connection without
# sending complete message body (incomplete chunked read)". Treated as final, each one
# ended the turn with an error; Claude Code recovered only by falling back to a
# non-streaming retry of the whole request.
_TRANSIENT_IN_BAND = re.compile(
    r"peer closed connection|incomplete chunked read|connection (?:reset|aborted|closed)|"
    r"server disconnected|remote ?protocol|temporarily unavailable|overloaded|"
    r"upstream connect error|timed? ?out|\b(?:502|503|504|520|521|522|523|524)\b",
    re.I,
)


def _is_transient(text: str) -> bool:
    return bool(_TRANSIENT_IN_BAND.search(text or "")) and not _is_over_length(text)


def _is_over_length(text: str) -> bool:
    """Whether an upstream error is Agentaus reporting the prompt as too long."""
    lowered = (text or "").lower()
    # Measured (d12 c1913): "This model's maximum context length is 131072 tokens. However,
    # ... your prompt contains 248801 input tokens" was not recognised, so the identical
    # request was sent again three times and the run ended on the error.
    return ("max_model_len" in lowered or "maximum context length" in lowered
            or "exceeds" in lowered and "prompt" in lowered)


# A timeout is only a size problem if the request does not fit. Anything at or under the
# window is a request Agentaus is able to process, so a 524 on it says the service is
# slow, not that the prompt is too big - and shrinking it treats the wrong illness.
#
# That leaves the three signals cleanly separated, which is the point:
#
#   * too big for the window   -> Agentaus says so explicitly ("exceeds max_model_len"),
#                                 and _is_over_length drives the recompaction.
#   * approaching the window   -> compaction has already run, proactively, at
#                                 AGENTAUS_COMPACT_THRESHOLD. Nothing should reach the
#                                 edge by surprise.
#   * a 524 under the window   -> the upstream is unwell. Say so.
#
# An earlier version used a quarter of the window, which was a number with nothing behind
# it: it classified a 3,642-token payload as "possibly too big" and spent eight minutes
# recompacting 3% of a context window while a six-token probe to the same endpoint was
# also timing out.
_REFIT_WORTH_TRYING_ABOVE = 1.0


def _worth_refitting(body: dict, limit: int) -> bool:
    """Whether a timeout could plausibly be about the size of this request.

    Distinguishes "the conversation does not fit" from "the upstream is unwell". Both
    arrive as HTTP 524 and only the first is something the bridge can act on.
    """
    if limit <= 0:
        return False
    return estimate_request_tokens(body) > limit * _REFIT_WORTH_TRYING_ABOVE


def _degraded_upstream_message(tokens: int, limit: int) -> str:
    """What to tell the user when a small request times out.

    Naming the size is the point: it says plainly that this is not their conversation
    being too long, so they do not go looking for something to compact.
    """
    return (
        f"Agentaus timed out on a small request ({tokens:,} tokens, against a "
        f"{limit:,}-token window), which means the service is slow or unavailable rather "
        f"than your conversation being too long. Nothing here needs compacting. Try "
        f"again shortly, or switch to a Claude model with /model in the meantime."
    )


def _is_too_slow(text: str) -> bool:
    """Whether an upstream failure means the prompt took too long, not that it was wrong.

    Agentaus sits behind Cloudflare, which answers 524 when the origin has not replied in
    time. For a large conversation that is the same actionable signal as an explicit
    over-length error: send less. The difference is that nothing in the response says so,
    so a plain retry replays the identical payload and times out identically - which is
    how a turn burned every retry it had and failed anyway.
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ("524", "522", "504", "gateway time"))


def _agentaus_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.agentaus_api_key}",
        "Content-Type": "application/json",
        # Agentaus answers 406 to an explicit "Accept: text/event-stream", so ask for
        # anything and let the `stream` body flag decide the response format.
        "Accept": "*/*",
    }


def _checks_apply(body: dict) -> bool:
    """Whether the bridge's answer-rewriting passes may touch this request at all.

    Only agent turns, which always carry tools. A tool-less request is Claude Code
    talking to the model for its own purposes - classifying whether a turn is blocked,
    naming the session - and the answer's exact shape is the point. See
    AGENTAUS_REVIEW_TOOLLESS.
    """
    return bool(body.get("tools")) or settings.agentaus_review_toolless


def _exact_command_response(body: dict, exact: tuple, model: str, wants_stream: bool,
                            started: float) -> Response:
    """The turn an exact-command graph agent would have taken, made without the model: the
    Bash call on its first request, the command's output as its reply on the second."""
    kind, value = exact
    if kind == "run":
        arguments = {"command": value, "description": "Run the graph's exact command",
                     "timeout": 600000}
        rlog(logging.INFO, "graph exact command: running it without the model (%s)",
             value.split("\n", 1)[0][:120])
        return _synthetic_response(body, model, wants_stream, started, "exact command",
                                   tool=("Bash", arguments))
    rlog(logging.INFO, "graph exact command: replying with its output (%d chars) without "
         "the model", len(value))
    return _synthetic_response(body, model, wants_stream, started, "exact command", text=value)


def _synthetic_response(body: dict, model: str, wants_stream: bool, started: float, why: str,
                        *, text: str | None = None, tool: tuple | None = None) -> Response:
    """A whole assistant turn made by the bridge - one text block (end_turn) or one tool call
    (tool_use) - streamed or not, as the client asked. No upstream call."""
    call_id = f"toolu_{uuid.uuid4().hex[:24]}"
    if wants_stream:
        builder = AnthropicStreamBuilder(model, input_tokens=estimate_request_tokens(body),
                                         chunk_chars=settings.chunk_chars)

        async def events() -> AsyncIterator[bytes]:
            yield builder.start()
            if tool is not None:
                yield builder.tool_use(call_id, tool[0], tool[1])
                yield builder.finish("tool_calls", None)
            else:
                yield builder.text(text or "")
                yield builder.finish("stop", None)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    content = ([{"type": "tool_use", "id": call_id, "name": tool[0], "input": tool[1]}]
               if tool is not None else [{"type": "text", "text": text or ""}])
    rlog(logging.INFO, "POST /v1/messages model=%s route=agentaus stream=false -> 200 in %.1fs "
         "(%s, no upstream call)", model, time.monotonic() - started, why)
    return JSONResponse({
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
        "model": model, "content": content,
        "stop_reason": "tool_use" if tool is not None else "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": estimate_request_tokens(body), "output_tokens": 0},
    })


# --------------------------------------------------------------------------------------
# Heartbeat: something Claude Code counts as progress while the bridge waits
# --------------------------------------------------------------------------------------

def _heartbeat(builder: AnthropicStreamBuilder | None, waited: float) -> bytes:
    """A thinking delta saying the turn is still waiting - or b"" when one would split a text or
    tool block the client is reading (pings cover that). Built by the producer, the one task
    that owns `builder`, so its block indexes stay in order."""
    if builder is None or builder.open_kind not in (None, "thinking"):
        return b""
    return builder.live_thinking(f"_Still waiting for Agentaus ({waited:.0f}s)._\n\n")


async def _waiting(aw, builder: AnthropicStreamBuilder | None, box: dict,
                   every: float | None = None) -> AsyncIterator[bytes]:
    """Await `aw`, putting its result in box["value"] (its exception propagates), and yield a
    heartbeat for every `every` seconds (BRIDGE_HEARTBEAT_SECONDS) it stays silent. Measured:
    Claude Code cuts a stream carrying only message_start and pings after 600 s (d14: 13 of 17
    disconnects at 600-640 s) and re-sends the turn without streaming; one thinking delta a
    minute kept a 650 s turn alive."""
    every = settings.heartbeat_seconds if every is None else every
    task = asyncio.ensure_future(aw)
    started = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=every if every > 0 else None)
            if done:
                break
            beat = _heartbeat(builder, time.monotonic() - started)
            if beat:
                yield beat
        box["value"] = task.result()
    finally:
        if not task.done():
            task.cancel()


async def _lines_beating(upstream: httpx.Response, builder: AnthropicStreamBuilder | None,
                         every: float | None = None) -> AsyncIterator[tuple[str, object]]:
    """An upstream stream's lines as ("line", text), with ("beat", bytes) between them while it
    is silent. The transport bounds the silence itself (AGENTAUS_STREAM_IDLE_S)."""
    lines = upstream.aiter_lines().__aiter__()

    async def following() -> tuple[bool, str]:
        # StopAsyncIteration must not escape into _waiting, an async generator itself.
        try:
            return True, await lines.__anext__()
        except StopAsyncIteration:
            return False, ""

    while True:
        box: dict = {}
        async for beat in _waiting(following(), builder, box, every):
            yield "beat", beat
        more, line = box["value"]
        if not more:
            return
        yield "line", line


GRAPH_WAITING = ("The workflow is running in the background; I will check its result when it "
                 "finishes.")


def _graph_launch(body: dict) -> dict | None:
    """The Workflow call that starts the agent graph, when an opted-in main loop has not
    started it and the newest message is the user's; None otherwise.

    Measured (d16 tender a5): the model read three files and wrote its answer in the chat -
    no Workflow call, no response.md, a 0.0. Whether the graph runs is the user's choice
    ("ultracode"), not the model's, so the bridge makes the call itself.
    """
    if not _graph_main_loop(body):
        return None
    if "Workflow" not in {(t or {}).get("name") for t in body.get("tools") or []}:
        return None
    messages = [m for m in body.get("messages") or [] if m.get("role") != "system"]
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Workflow"
                for b in content):
            return None
    if not messages or messages[-1].get("role") != "user":
        return None
    last = messages[-1].get("content")
    typed = _without_reminders(last) if isinstance(last, str) else _without_reminders("\n".join(
        b.get("text", "") for b in last or [] if isinstance(b, dict) and b.get("type") == "text"))
    if not typed or "<task-notification>" in typed:
        return None
    first, latest = _original_request(body), _last_user_text(body)
    task = first if not latest or latest == first else f"{first}\n\nFollow-up:\n{latest}"
    cwd = working_directory_of(body)
    if not task.strip() or not graph.suits(task, cwd):
        return None
    call = graph.expand({"name": "Workflow", "arguments": {"description": "Agentaus agent graph"}},
                        task, cwd)
    arguments = call.get("arguments")
    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(arguments, dict) or not arguments.get("script"):
        return None
    graph.remember_run(body, task, latest)
    return arguments


def _graph_waiting_reply(body: dict) -> str | None:
    """The reply to a graph main loop's "Workflow launched in background" turn, or None.

    Measured (d12 c1913): told to end its turn, the model ran `ls -R ~/.claude/projects`
    (prompted by the launch result's "Transcript dir" line), got 705 KB, and the conversation
    never fitted the window again - the graph's result was never read. Only when the newest
    user message is that launch's result and holds nothing the user typed.
    """
    if not _graph_main_loop(body) or not awaiting_background(body):
        return None
    messages = [m for m in body.get("messages") or [] if m.get("role") != "system"]
    if len(messages) < 2 or messages[-1].get("role") != "user":
        return None
    content, previous = messages[-1].get("content"), messages[-2].get("content")
    uses = [b for b in previous or [] if isinstance(b, dict) and b.get("type") == "tool_use"] \
        if isinstance(previous, list) else []
    if not uses or any(u.get("name") != "Workflow" for u in uses) or not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "text" and _without_reminders(b.get("text", ""))
           for b in content):
        return None
    return GRAPH_WAITING


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
    # The body as the client sent it. `body` is rebound to the fitted one below, and a
    # compacted body has lost the turns the guards and the graph's task are read from.
    received = body

    # An agent that runs one exact command is played by the bridge. See graph.exact_command.
    exact = graph.exact_command(body) if is_subagent_turn(body) else None
    if exact is not None:
        return _exact_command_response(body, exact, display_model, wants_stream, started)
    if _graph_main_loop(body):
        # Fold this request's launches and reports into the session's graph record first.
        _graph_session(body)
        launch = _graph_launch(body)
        if launch:
            rlog(logging.INFO, "graph main loop: no workflow has run; launching the agent "
                 "graph without the model")
            return _synthetic_response(body, display_model, wants_stream, started,
                                       "graph launch", tool=("Workflow", launch))
        waiting = _graph_waiting_reply(body)
        if waiting:
            rlog(logging.INFO, "graph main loop: the workflow is launched; replying without "
                 "the model")
            return _synthetic_response(body, display_model, wants_stream, started,
                                       "workflow launched", text=waiting)

    # How far past its time a graph agent is, read off the request as received: fitting and
    # compaction rewrite the messages the agent is known by. See graph.overdue.
    clock = {"over": 0.0}
    graph_agent = graph.is_graph_task(body) and is_subagent_turn(body)
    if graph_agent:
        over, news = graph.overdue(body)
        clock["over"] = over
        if over > settings.agentaus_graph_give_up_s:
            # The graph has stopped waiting for this agent, and the agent still holds a slot in
            # the harness. Measured (d13, d14): stuck agents ran 68-81 min before Claude Code
            # failed them; one request was retried for 2,627 s. The graph reads this short reply
            # as no answer.
            rlog(logging.WARNING, "graph agent (%s) is %.0fs past its %ds limit; giving up on "
                 "it without an upstream call", graph.role_of(body) or "?", over,
                 graph.agent_limit(body))
            return _synthetic_response(body, display_model, wants_stream, started,
                                       "graph agent past its limit", text=graph.OUT_OF_TIME)
        if over and news:
            rlog(logging.WARNING, "graph agent (%s) is %.0fs past its %ds limit; "
                 "asking for its result with no tools", graph.role_of(body) or "?",
                 over, graph.agent_limit(body))

    def stop_retrying() -> bool:
        """Whether a gateway failure on this turn is answered by the bridge, not replayed: a
        time-up turn (its 524s are about the request - see throttle.REQUEST_KIND), or a graph
        agent that has run past its limit while its request was being retried."""
        return graph_agent and (clock["over"] > 0 or graph.seconds_over(received) > 0)

    def time_up_text() -> str:
        rlog(logging.WARNING, "graph agent (%s): a gateway failure past its limit; answering "
             "with what it already has", graph.role_of(received) or "?")
        return graph.time_up_fallback(received)

    request_kind = "time_up" if clock["over"] else "normal"
    # Decided on the request as received, before compaction can drop the opt-in.
    plain_text = _writes_plain_text(body)

    # Written once and reused across refits: a refit recompacts the same turn, and
    # re-planning it would pay for the same call again to get the same plan.
    plan_holder: dict = {"text": None}
    # The library skills prepare() offered - the calls the bridge must answer itself.
    library_holder: dict = {"names": frozenset()}

    async def prepare(
        current: dict, scale: float = 1.0, on_plan_piece=None, on_note=None
    ) -> tuple[dict, dict]:
        """Fit to the window, add the guidance and the plan, and build the payload.

        Returns (body, payload). Compaction can take a minute or more on a long
        conversation, which is why the streaming path runs this *inside* the response
        generator rather than before it - see the note there.
        """
        # A `.docx` read by Claude Code's own Read tool arrives as zip noise, because
        # that tool runs on the client and a `.docx` is a zip. Fix it before anything
        # else looks at it - distillation would otherwise spend model calls condensing
        # binary, and the model would answer from nothing.
        #
        # Both of these read files - the first may run LibreOffice - so they run in a
        # worker thread. On the event loop they would stall every other request.
        if settings.agentaus_office_extract:
            current = await asyncio.to_thread(documents.repair_tool_results, current)

        # A preview standing in for a 1.6 MB listing is worse than useless - the model
        # answers from a fragment and fills the rest in. The client saved the real output
        # to disk and the bridge is on the same machine, so read it.
        current = await asyncio.to_thread(
            persisted.restore, current,
            int(limit * settings.agentaus_restore_window_share) if limit > 0 else None)

        # An Edit refused as "not found" gets the closest real lines, indentation exact, so the
        # next try copies them instead of re-reading. See repair.edit_hints.
        if settings.agentaus_edit_repair:
            current, hinted = await asyncio.to_thread(repair.edit_hints, current)
            if hinted:
                rlog(logging.INFO, "edit repair: showed the closest lines for %d refused "
                     "Edit(s)", hinted)
        # A graph main loop's .py edit that broke importing the module is said so in its
        # result. See import_guard.py.
        if settings.agentaus_import_guard and _graph_main_loop(current):
            current, broke = await asyncio.to_thread(import_guard.check, current)
            if broke:
                rlog(logging.WARNING, "import guard: %d edit(s) broke a module's import", broke)

        # The graph script the bridge put in a Workflow call is ~115 KB, resent upstream on
        # every later main-loop request. The model never needs it again.
        if not is_subagent_turn(current):
            current, swapped = _without_graph_script(current)
            if swapped:
                rlog(logging.INFO, "workflow: sent %d graph call(s) upstream without the script",
                     swapped)

        # Claude Code asks every model it does not know to credit Claude in its commits.
        current, credited = attribution.credit(current, settings.agentaus_coauthor)
        if credited:
            rlog(logging.INFO, "attribution: commits credit %s", settings.agentaus_coauthor)

        # Claude Code lists project skills by bare name; put back what each is for, so
        # the model - and the planner, which reads the same listing - can choose one.
        # Before fitting, so the window estimate counts the descriptions.
        if settings.agentaus_skill_routing:
            current, described = with_skill_descriptions(current)
            if described:
                rlog(logging.INFO, "skill listing: described %d project skill(s)", described)
        if settings.agentaus_skill_library:
            current, served = with_library_skills(current)
            library_holder["names"] = served
            if served:
                rlog(logging.INFO, "skill listing: offered %d library skill(s)", len(served))

        # Condense oversized tool results BEFORE fitting. Order matters: this is what
        # decides whether compaction is needed at all, and compacting first would
        # summarise output that was about to be condensed anyway.
        if settings.agentaus_distill_results:
            raw = estimate_request_tokens(current)
            current = await _get_distiller(request).distill(current)
            shrunk = estimate_request_tokens(current)
            if shrunk < raw:
                rlog(logging.INFO, "distillation: %d -> %d est tokens", raw, shrunk)

        # The ledger is built here, from the message list as it stands *before*
        # compaction. That is the whole point: the calls that fall out of the window are
        # the ones the model forgets it already made.
        if graph.is_graph_task(current):
            current, cut = graph.trim_results(current)
            if cut:
                rlog(logging.INFO, "graph subagent: cut %d oversized tool result(s)", cut)

        ledger_source = list(current.get("messages") or [])
        # Same reason: compaction can summarise away the "ultracode: ..." that opted in.
        opted_in = workflow_opted_in(current)

        before = estimate_request_tokens(current)
        if before + reserved > int(limit * settings.agentaus_compact_threshold):
            if on_note is not None:
                on_note(f"compacting the earlier conversation to fit Agentaus' "
                        f"{limit:,}-token window (~{before:,} tokens now)")
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
        # Offer the bridge's own search and steer Grep towards literal lookups. Done
        # here rather than before compaction so the tool list travels with whatever
        # survived the fit; it costs ~250 tokens the estimate does not see, which is
        # immaterial against a 0.8 threshold.
        if settings.agentaus_tool_focus:
            fitted, dropped = focus_tools(fitted, workflow_opt_in=opted_in)
            if graph.is_graph_task(fitted):
                fitted, more = graph.focus(fitted)
                dropped = dropped + more
            if dropped:
                rlog(logging.INFO, "tool focus: offering %d tool(s), holding back %d (%s)",
                     len(fitted.get("tools") or []), len(dropped), ", ".join(dropped)[:200])

        offered = []
        # Inventory first in the list as well as first in the workflow: a question about
        # a whole tree should start here, and a model reads the tools in order.
        if settings.agentaus_inventory:
            offered.append(bridge_tools.INVENTORY_SCHEMA)
        if settings.agentaus_search:
            offered.append(bridge_tools.SEARCH_SCHEMA)
        if settings.agentaus_web_search:
            offered.append(bridge_tools.WEB_SEARCH_SCHEMA)
        if settings.agentaus_investigate:
            offered.append(bridge_tools.INVESTIGATE_SCHEMA)
        if settings.agentaus_zoom:
            offered.append(bridge_tools.ZOOM_SCHEMA)
        # Not for a graph subagent: the graph already chose its context, and one
        # `agentaus_search` fans out into dozens of model calls - measured, hundreds of a
        # graph run's calls against an upstream that serves about thirty a minute.
        if offered and not graph.is_graph_task(fitted):
            fitted = inject_bridge_tools(fitted, offered)

        if settings.agentaus_tool_ledger:
            fitted = {**fitted, "system": with_ledger(
                fitted.get("system"), ledger_source,
                limit=settings.agentaus_tool_ledger_limit,
            )}

        # Supplement the system prompt for Agentaus only. Claude turns never reach
        # here - they are forwarded untouched by _passthrough.
        if settings.agentaus_guidance:
            fitted = {**fitted, "system": with_guidance(
                fitted.get("system"), fitted, routing=settings.agentaus_skill_routing,
                workflow_opt_in=opted_in)}

        # A graph subagent's prompt already carries exactly the context the graph chose;
        # the whole request on top would undo the point of a small prompt.
        if is_subagent_turn(fitted) and not graph.is_graph_task(fitted):
            parent = sessions.request_of(sessions.session_of(request.headers))
            if parent:
                fitted = {**fitted, "system": with_parent_request(fitted.get("system"), parent)}
                rlog(logging.INFO, "subagent: shown its session's request (%d chars)",
                     len(parent))

        # Measured (d12 c1913): the planning pass took 254 s and failed with 524 before a
        # graph main loop's first turn reached the model - a turn whose job is to call Workflow.
        if settings.agentaus_thinking and should_think(fitted) and not _graph_not_started(received):
            if plan_holder["text"] is None:
                with _Phase("planning"):
                    plan_holder["text"] = await _plan_turn(client, fitted, on_plan_piece)
            fitted = {**fitted, "system": with_plan(fitted.get("system"), plan_holder["text"])}

        # A graph agent past its time is asked for its result, with no tools to keep going.
        # Last, so nothing after it puts tools back. The clock was read off the request as
        # received, above. See graph.overdue.
        if clock["over"]:
            fitted = graph.time_up(fitted)

        built = anthropic_request_to_agentaus(
            fitted,
            system_prompt_overwrite=settings.system_prompt_overwrite,
            # Streamed upstream whatever the client asked: see config.upstream_stream.
            stream=settings.upstream_stream,
        )
        if settings.log_bodies:
            rlog(logging.INFO, "-> agentaus payload: %s", json.dumps(built)[: settings.log_body_chars])
        return fitted, built

    async def run_buffered() -> Response:
        nonlocal body
        body, payload = await prepare(body)
        scale = 1.0
        data: dict = {}

        # Rounds of bridge-executed tool calls. Each round runs the tool, appends its
        # result, and asks Agentaus again. The loop ends when the model answers with
        # prose, or with a tool only Claude Code can run.
        corrections = 0
        ran: dict = {}
        for tool_round in range(settings.agentaus_tool_rounds + 1):
            # Agentaus is the authority on whether a prompt fits. If it says no, it also
            # says what its real limit is, so the next attempt compacts against that
            # instead of against our estimate.
            for fit_attempt in range(settings.agentaus_fit_attempts + 1):
                try:
                    upstream = await _post_with_retry(
                        client,
                        settings.agentaus_url,
                        json_body=payload,
                        headers=_agentaus_headers(),
                        stream_upstream=bool(payload.get("stream")),
                        kind=request_kind,
                        give_up=stop_retrying,
                    )
                except httpx.HTTPError as exc:
                    if stop_retrying():
                        return _synthetic_response(body, display_model, False, started,
                                                   "time up", text=time_up_text())
                    return _error_response(502, "api_error", f"Agentaus request failed: {exc}")

                # A timeout is treated like an over-length rejection: both mean the
                # next attempt must send less, and neither is fixed by replaying.
                over_length = upstream.status_code >= 400 and (
                    _is_over_length(upstream.text)
                    or (_is_too_slow(upstream.text) and _worth_refitting(body, limit))
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
                    stream=settings.upstream_stream,
                )

            if upstream.status_code >= 400:
                if upstream.status_code in _RETRYABLE_STATUS and stop_retrying():
                    return _synthetic_response(body, display_model, False, started,
                                               "time up", text=time_up_text())
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

            known = _known_tool_names(payload)
            calls = _openai_calls(data)
            if not calls:
                data, calls = _recover_harmony_calls(data)
            mine, _theirs, invented = _partition_tool_calls(
                calls, known, library_holder["names"])
            if invented and corrections < settings.agentaus_correction_rounds:
                corrections += 1
                rlog(logging.WARNING, "model invented %d tool name(s): %s; correcting",
                     len(invented), ", ".join(c["name"] for c in invented))
                payload = _with_tool_results(
                    payload, invented, [_correction_for(c, known) for c in invented])
                continue
            # The guards and the graph's task read the body as the client sent it, not the
            # compacted one - see _graph_reruns.
            reruns = _graph_reruns(_theirs, received)
            # In a worker thread: preparing the graph copies the project and reads its
            # documents - measured, 9s for fourteen PDFs - and would stall every other request.
            await asyncio.to_thread(_run_graph_instead,
                                    [c for c in _theirs if all(c is not r for r, _ in reruns)],
                                    received)
            if plain_text and graph.plain_text_writes(_theirs):
                rlog(logging.INFO, "plain text: normalised characters in a text-file write")
            cwd = working_directory_of(received)
            broken = _validate_tool_calls(mine + _theirs, _tool_schemas(payload), cwd)
            broken += reruns
            broken += early_deliverable_writes(_theirs, received, cwd)
            if _graph_main_loop(received):
                broken += writes_before_workflow(_theirs, received, cwd)
            broken += graph.guarded_writes(_theirs, received, cwd)
            if broken and corrections < settings.agentaus_correction_rounds:
                corrections += 1
                rlog(logging.WARNING, "%d malformed tool call(s): %s; correcting",
                     len(broken), "; ".join(m for _, m in broken)[:300])
                payload = _with_tool_results(
                    payload, [c for c, _ in broken], [m for _, m in broken])
                continue
            if broken:
                rlog(logging.WARNING, "forwarding %d malformed tool call(s) - out of "
                     "rounds to correct them", len(broken))
            _guard_imports(_theirs, received, cwd)
            data = _with_coerced_calls(data, mine + _theirs)
            if not mine:
                if invented:
                    rlog(logging.WARNING, "dropping %d invented tool call(s) - out of "
                         "rounds to correct them", len(invented))
                    data = _without_unknown_calls(data, known)
                break
            if tool_round < settings.agentaus_tool_rounds:
                rlog(logging.INFO, "running %d bridge tool call(s): %s",
                     len(mine), ", ".join(c["name"] for c in mine))
                payload = await _run_bridge_tools(
                    client, payload, mine, working_directory_of(body), ran)
                continue
            # Out of rounds with a bridge tool still pending. It must not reach Claude
            # Code, which has never heard of it and would fail the tool_use outright.
            rlog(logging.WARNING,
                 "tool round limit (%d) reached; dropping %d unanswered bridge call(s)",
                 settings.agentaus_tool_rounds, len(mine))
            # Everything those searches found is still in the payload, so ask again with
            # the tools removed rather than surfacing an apology for having searched.
            forced = await _answer_without_tools(client, payload)
            data = _with_forced_answer(data, forced) if forced else _without_bridge_calls(data)
            break

        choice0 = (data.get("choices") or [{}])[0]
        msg0 = choice0.get("message") or {}

        # An empty reply is never a valid answer to a question. Observed as
        # "200 in 94.8s in=0 out=0" on a question the model had every means to answer.
        if not (msg0.get("content") or "").strip() and not msg0.get("tool_calls"):
            rlog(logging.WARNING, "upstream returned an empty answer; asking once more")
            retried = await _answer_without_tools(client, payload)
            if retried:
                data = _with_forced_answer(data, retried)
                choice0 = (data.get("choices") or [{}])[0]
                msg0 = choice0.get("message") or {}

        if (settings.agentaus_syntax_check and not msg0.get("tool_calls")
                and not is_subagent_turn(body)
                and (msg0.get("content") or "").strip()):
            parsed = await _fix_syntax(client, msg0["content"])
            if parsed != msg0.get("content"):
                msg0 = {**msg0, "content": parsed}
                data = {**data, "choices": [{**choice0, "message": msg0}]
                        + list((data.get("choices") or [])[1:])}

        # Review only a plain text answer. A turn that calls tools is mid-task, and
        # rewriting it would break the tool_use the client is waiting on.
        if (
            settings.agentaus_self_review
            and _checks_apply(body)
            and not msg0.get("tool_calls")
            and not ran
            and worth_reviewing_turn(body)
            and worth_reviewing(msg0.get("content") or "",
                                min_chars=settings.agentaus_review_min_chars)
        ):
            reviewed = await _self_review(client, _last_user_text(body), msg0["content"])
            if reviewed != msg0["content"]:
                msg0 = {**msg0, "content": reviewed}
                data = {**data, "choices": [{**choice0, "message": msg0}]
                        + list((data.get("choices") or [])[1:])}

        if (
            settings.agentaus_grounding_check
            and _checks_apply(body)
            and not msg0.get("tool_calls")
            and (ran or not worth_reviewing_turn(body))
            and _grounding_applies(body, msg0.get("content") or "", ran)
        ):
            grounded = await _check_grounding(client, body, msg0["content"], ran)
            if grounded != msg0.get("content"):
                msg0 = {**msg0, "content": grounded}
                data = {**data, "choices": [{**choice0, "message": msg0}]
                        + list((data.get("choices") or [])[1:])}

        message = agentaus_response_to_anthropic(
            data,
            model=display_model,
            thinking=plan_holder["text"] if settings.agentaus_thinking_visible else None,
        )
        rlog(logging.INFO,
            "POST /v1/messages model=%s route=agentaus stream=false -> 200 in %.1fs in=%s out=%s",
            display_model,
            time.monotonic() - started,
            message["usage"]["input_tokens"],
            message["usage"]["output_tokens"],
        )
        return JSONResponse(message)

    if not wants_stream:
        # A buffered client: Claude Code's fallback after a cut stream, or its re-send of one.
        # It joins the turn already running for the same request - the stream it replaces, or
        # an earlier buffered copy - and waits BRIDGE_NONSTREAM_DEADLINE at most. See flight.py.
        return await _answer_buffered(request, flight.key_of(received, request.headers),
                                      run_buffered, display_model)

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
            # message_start goes out at once, whatever happens next: what the client sees until
            # the answer is that, the plan or notes as they are written, and a heartbeat while
            # nothing else comes (see _waiting).
            builder = AnthropicStreamBuilder(
                display_model, input_tokens=estimate_request_tokens(body),
                chunk_chars=settings.chunk_chars,
            )
            yield builder.start()
            streamed_plan = False
            if settings.agentaus_live_draft and settings.agentaus_thinking_visible:
                # What preparation produces - the plan as it is written, a note when
                # compaction starts - streams into a live thinking block. Before this the
                # client saw nothing but pings until planning finished, so its token counter
                # sat at zero for 5-15 seconds on every agent turn.
                pieces: asyncio.Queue = asyncio.Queue()
                seen_plan = []

                def on_plan_piece(text: str) -> None:
                    seen_plan.append(text)
                    pieces.put_nowait(text)

                def on_note(text: str) -> None:
                    pieces.put_nowait(f"_{text}_\n\n")

                work = asyncio.ensure_future(prepare(body, 1.0, on_plan_piece, on_note))
                quiet_since = time.monotonic()
                try:
                    while not (work.done() and pieces.empty()):
                        try:
                            piece = await asyncio.wait_for(pieces.get(), timeout=0.25)
                        except asyncio.TimeoutError:
                            quiet = time.monotonic() - quiet_since
                            if 0 < settings.heartbeat_seconds <= quiet:
                                beat = _heartbeat(builder, time.monotonic() - started)
                                if beat:
                                    yield beat
                                quiet_since = time.monotonic()
                            continue
                        quiet_since = time.monotonic()
                        yield builder.live_thinking(piece)
                finally:
                    if not work.done():
                        work.cancel()
                prepared_body, prepared_payload = work.result()
                streamed_plan = bool(seen_plan)
                if plan_holder["text"] and not streamed_plan:
                    # Planned, but the stream fell back to a buffered call: show it whole.
                    yield builder.live_thinking(plan_holder["text"])
                    streamed_plan = True
            else:
                box: dict = {}
                async for beat in _waiting(prepare(body), builder, box):
                    yield beat
                prepared_body, prepared_payload = box["value"]

            async def refit(scale: float) -> dict:
                _, rebuilt = await prepare(prepared_body, scale)
                return rebuilt

            rlog(logging.INFO, "upstream start (est %d tok)",
                 estimate_request_tokens(prepared_body))
            async for chunk in _agentaus_event_stream(
                client, prepared_payload, prepared_body, display_model, started, refit,
                plan=None if streamed_plan else plan_holder["text"], builder=builder,
                library=library_holder["names"], plain_text=plain_text,
                client_body=received, stop_retrying=stop_retrying,
                time_up_text=time_up_text, request_kind=request_kind,
            ):
                yield chunk
        except asyncio.CancelledError:
            # No client is left, or the turn ran out its deadline - flight.py logs which.
            rlog(logging.WARNING, "turn stopped after %.1fs", time.monotonic() - started)
            raise
        except GeneratorExit:
            rlog(logging.WARNING, "stream closed early after %.1fs",
                 time.monotonic() - started)
            raise

    # The turn runs as a flight: a client that leaves does not stop it at once, and an
    # identical request - Claude Code's re-send, or its buffered fallback - joins it instead of
    # starting another. The flight holds the BRIDGE_TURN_TIMEOUT deadline. See flight.py.
    key = flight.key_of(received, request.headers)
    running = flight.find(key, flight.STREAM)
    if running is not None:
        rlog(logging.INFO, "streamed request joins the turn already running for it (%.0fs in)",
             time.monotonic() - running.started)
    else:
        running = flight.start_stream(key, stream_with_compaction(),
                                      deadline=settings.turn_timeout_seconds)
    generator = _keepalive(
        _relay(running),
        settings.ping_interval_seconds,
        AnthropicStreamBuilder.ping,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _relay(running: "flight.Flight") -> AsyncIterator[bytes]:
    """A streamed client's view of its turn's flight. Its leaving is worth a line: without it
    a turn the user abandoned looks identical to one still in flight."""
    try:
        async for chunk in flight.subscribe(running):
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        if not running.done:
            rlog(logging.WARNING, "client disconnected after %.1fs; the turn keeps running "
                 "for a retry to join", time.monotonic() - running.started)
        raise


async def _answer_buffered(request: Request, key: str, run, model: str) -> Response:
    """A buffered client's answer: from the turn already running for its request, or from a
    new one, waiting BRIDGE_NONSTREAM_DEADLINE at most and no longer than the client stays.

    Measured (d11, d14): abandoned buffered requests ran up to 8,107 s, each re-send queued for
    a slot and made up to five attempts, and one first turn's graph went to a closed socket."""
    joined = flight.find(key)
    if joined is not None:
        rlog(logging.INFO, "buffered request joins the %s turn already running for it "
             "(%.0fs in)", joined.kind, time.monotonic() - joined.started)
    else:
        joined = flight.start_buffered(key, run, deadline=settings.turn_timeout_seconds)
    deadline = settings.nonstream_deadline_seconds
    gone = request.is_disconnected
    if joined.kind == flight.STREAM:
        events = await flight.collect_stream(joined, deadline, gone)
        if events is not None:
            status, message = flight.assemble(events)
            if status == 200:
                rlog(logging.INFO, "POST /v1/messages model=%s route=agentaus stream=false -> "
                     "200 (assembled from the streamed turn)", model)
            return JSONResponse(message, status_code=status)
    else:
        result = await flight.wait_buffered(joined, deadline, gone)
        if result is not None:
            status, raw, media = result
            return Response(raw, status_code=status, media_type=media)
    rlog(logging.WARNING, "buffered request: answering after %.0fs that the turn is still "
         "running; a re-send will join it", time.monotonic() - joined.started)
    return _error_response(
        504, "api_error",
        f"The Agentaus turn is still running ({time.monotonic() - joined.started:.0f}s so "
        "far). The bridge keeps it going: retry the request and it will get this turn's answer.")


async def _agentaus_event_stream(
    client: httpx.AsyncClient,
    payload: dict,
    original: dict,
    model: str,
    started: float,
    refit=None,
    plan: str | None = None,
    builder: AnthropicStreamBuilder | None = None,
    library: frozenset = frozenset(),
    plain_text: bool = False,
    client_body: dict | None = None,
    stop_retrying: Callable[[], bool] | None = None,
    time_up_text: Callable[[], str] | None = None,
    request_kind: str = "normal",
) -> AsyncIterator[bytes]:
    """Produce a valid Anthropic SSE stream from an Agentaus response.

    `builder` is passed when the response has already started - message_start sent and
    the plan streamed live - so this continues it rather than beginning a second one.
    `original` is the prepared (fitted, possibly compacted) body; `client_body` the body as
    the client sent it, which the write guards and the graph's task are read from.

    `stop_retrying()` True means a gateway failure is not replayed: the turn is answered with
    `time_up_text()` instead (a graph agent past its limit - see _handle_agentaus).
    `request_kind` is what this turn is to the throttle (throttle.REQUEST_KIND).
    """
    received = client_body if client_body is not None else original
    if builder is None:
        builder = AnthropicStreamBuilder(
            model,
            input_tokens=estimate_request_tokens(original),
            chunk_chars=settings.chunk_chars,
        )
        yield builder.start()

    # The plan leads the message, where a native thinking block would. Emitted before
    # the upstream call rather than after it, so the user sees the model's reasoning
    # while the answer is still being generated instead of all at once at the end.
    if plan:
        # Either way the reasoning is visible. A thinking block is the better rendering;
        # tagged text is the fallback for a client that will not show one.
        yield (builder.thinking(plan) if settings.agentaus_thinking_visible
               else builder.plan_as_text(plan))

    finish_reason: str | None = None
    usage: dict | None = None
    accumulator = ToolCallAccumulator()

    # A transient fault before the first content byte can be retried invisibly: the
    # client has only seen `message_start`, which carries no content. Once any text or
    # tool call has been emitted a retry would duplicate it, so `emitted` latches the
    # stream to fail-fast from that point on.
    # Buffer the answer when it may need revising. Tool turns are never buffered:
    # they carry no prose to review and the client is waiting on the tool_use.
    # Held back only when a check may rewrite it; anything else streams as it arrives.
    buffering = settings.agentaus_self_review and _checks_apply(original)
    pending: list[str] = []

    # While the answer is held back, its tokens still go to the client as they arrive,
    # in a thinking block - see AnthropicStreamBuilder.live_thinking.
    live = buffering and settings.agentaus_live_draft
    live_started = False

    def draft(text: str) -> bytes:
        nonlocal live_started
        if not live or not text:
            return b""
        if not live_started:
            live_started = True
            lead = "\n\n" if builder.open_kind == "thinking" else ""
            text = lead + "_Live draft from Agentaus. The checked answer follows._\n\n" + text
        return builder.live_thinking(text)

    def progress(note: str) -> bytes:
        # A line in the draft saying what the bridge is doing between upstream calls, so
        # a long search reads as work in progress rather than a stalled turn.
        return draft(f"\n\n_{note}_\n\n") if live else b""

    # Tracks recompaction attempts driven by Agentaus rejecting the prompt as too long.
    fit_attempt = 0
    fit_scale = 1.0

    # Rounds of bridge-executed tool calls. `agentaus_search` is answered here and
    # re-asked upstream; the client never learns the call happened. Bounded, so a
    # model that keeps searching still has to produce an answer eventually.
    tool_round = 0
    corrections = 0
    ran: dict = {}
    # The tool_choice to put back after a stall re-ask that forced one. See below.
    forced_from = None

    def giving_up() -> bool:
        return stop_retrying is not None and stop_retrying()

    def time_up_answer() -> list:
        text = time_up_text() if time_up_text is not None else graph.TIME_UP_PARTIAL
        return [builder.text(text), builder.finish("stop", None)]

    # Whether this request has had a gateway failure: its retries are evidence about it, not
    # about the upstream (throttle.REQUEST_KIND).
    failed_once = False
    while True:
        attempt = 0
        while True:
            emitted = False
            pending = []
            finish_reason = None
            usage = None
            accumulator = ToolCallAccumulator()
            retry_reason: str | None = None
            throttle.REQUEST_KIND.set(
                "retry" if failed_once and request_kind == "normal" else request_kind)

            try:
                if payload.get("stream"):
                    async with _watching_for_stall("upstream stream"):
                        opening = client.stream("POST", settings.agentaus_url, json=payload,
                                                headers=_agentaus_headers())
                        box: dict = {}
                        # Waiting for the first byte - the throttle's queue, then up to 125 s
                        # before a 524 - is the long silence the heartbeat is for.
                        async for beat in _waiting(opening.__aenter__(), builder, box):
                            yield beat
                        upstream = box["value"]
                        try:
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
                                elif upstream.status_code in _RETRYABLE_STATUS and giving_up():
                                    # A time-up turn, or an agent now past its limit: the same
                                    # prompt would fail the same way. See throttle.REQUEST_KIND.
                                    rlog(logging.WARNING, "HTTP %d on a graph agent past its "
                                         "limit; not replaying it", upstream.status_code)
                                    for piece in time_up_answer():
                                        yield piece
                                    return
                                elif (
                                    upstream.status_code in _RETRYABLE_STATUS
                                    and attempt < settings.max_retries
                                ):
                                    failed_once = True
                                    retry_reason = f"HTTP {upstream.status_code}"
                                else:
                                    yield builder.error(
                                        f"Agentaus returned HTTP {upstream.status_code}: {detail}",
                                        _error_type_for_status(upstream.status_code),
                                    )
                                    yield builder.finish("stop", None)
                                    return
                            else:
                                async for kind, line in _lines_beating(upstream, builder):
                                    if kind == "beat":
                                        yield line
                                        continue
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
                                        message = str(chunk["error"].get("message") or "")
                                        # Nothing final has reached the client yet, so both
                                        # of these can be replayed invisibly.
                                        if not emitted and _is_transient(message) and giving_up():
                                            for piece in time_up_answer():
                                                yield piece
                                            return
                                        if (not emitted and _is_transient(message)
                                                and attempt < settings.max_retries):
                                            rlog(logging.WARNING, "agentaus in-band transient "
                                                 "error, retrying: %s", message[:200])
                                            failed_once = True
                                            retry_reason = f"upstream dropped ({message[:60]})"
                                            break
                                        if (not emitted and _is_over_length(message)
                                                and refit is not None
                                                and fit_attempt < settings.agentaus_fit_attempts):
                                            _learn_limit_from(message)
                                            fit_attempt += 1
                                            fit_scale *= settings.agentaus_fit_shrink
                                            payload = await refit(fit_scale)
                                            retry_reason = (f"prompt too long (in-band), "
                                                            f"refitting to {fit_scale:.0%}")
                                            break
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
                                                # already on screen cannot be revised. Its
                                                # tokens are still shown live, as a draft.
                                                pending.append(text)
                                                live_bytes = draft(text)
                                                if live_bytes:
                                                    yield live_bytes
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
                        finally:
                            await opening.__aexit__(None, None, None)
                else:
                    box = {}
                    async for beat in _waiting(_post_with_retry(
                        client,
                        settings.agentaus_url,
                        json_body={**payload, "stream": False},
                        headers=_agentaus_headers(),
                        kind=request_kind,
                        give_up=stop_retrying,
                    ), builder, box):
                        yield beat
                    upstream = box["value"]
                    if upstream.status_code >= 400 and (
                            upstream.status_code in _RETRYABLE_STATUS and giving_up()):
                        for piece in time_up_answer():
                            yield piece
                        return
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
                            live_bytes = draft(message["content"])
                            if live_bytes:
                                yield live_bytes
                        else:
                            emitted = True
                            yield builder.text(message["content"])
                    accumulator.add(
                        [{"index": i, **call} for i, call in enumerate(message.get("tool_calls") or [])]
                    )
                    finish_reason = choice.get("finish_reason")
                    usage = data.get("usage")

            except httpx.HTTPError as exc:
                if not emitted and _is_retryable_exception(exc) and giving_up():
                    rlog(logging.WARNING, "agentaus stream failed on a graph agent past its "
                         "limit (%s); not replaying it", _describe(exc))
                    for piece in time_up_answer():
                        yield piece
                    return
                failed_once = True
                if emitted or not _is_retryable_exception(exc) or attempt >= settings.max_retries:
                    rlog(logging.WARNING, "agentaus stream failed: %s", exc)
                    yield builder.error(f"Agentaus request failed: {exc}", "api_error")
                    yield builder.finish("stop", None)
                    return
                retry_reason = _describe(exc)

            if retry_reason:
                if live_started:
                    yield progress(f"retrying: {retry_reason}")
                await _sleep_before_retry(attempt, retry_reason)
                attempt += 1
                continue
            break

        calls = accumulator.drain()
        if not calls and pending:
            cleaned, recovered = harmony.extract("".join(pending))
            if recovered:
                pending = [cleaned] if cleaned.strip() else []
                calls = recovered
        if forced_from is not None:
            payload = {k: v for k, v in payload.items() if k != "tool_choice"}
            if forced_from != "(none)":
                payload["tool_choice"] = forced_from
            forced_from = None
        known = _known_tool_names(payload)
        mine, theirs, invented = _partition_tool_calls(calls, known, library)
        if invented and corrections < settings.agentaus_correction_rounds:
            # Correct it and ask again rather than failing a tool the client cannot run.
            # Deliberately NOT charged to tool_round: a correction is the bridge fixing
            # its own upstream's mistake, and spending the budget for real work on it
            # is what made a live turn run out before it could answer.
            corrections += 1
            rlog(logging.WARNING, "model invented %d tool name(s): %s; correcting",
                 len(invented), ", ".join(c["name"] for c in invented))
            rlog(logging.INFO, "invented call(s) as sent: %s",
                 "; ".join(f"{c['name']}({str(c.get('arguments'))[:200]})" for c in invented))
            yield progress("correcting a call to a tool that does not exist")
            payload = _with_tool_results(
                payload, invented, [_correction_for(c, known) for c in invented])
            pending = []
            continue
        if invented:
            rlog(logging.WARNING, "dropping %d invented tool call(s) - out of rounds",
                 len(invented))
        # A well-named call with malformed arguments is rejected by the client with a
        # wall of validator internals, which costs the turn and tells the model nothing.
        # Same budget as an invented name: it is the same class of mistake.
        # The guards and the graph's task read the body as the client sent it, not the
        # compacted one - see _graph_reruns.
        reruns = _graph_reruns(theirs, received)
        await asyncio.to_thread(_run_graph_instead,
                                [c for c in theirs if all(c is not r for r, _ in reruns)],
                                received)
        if plain_text and graph.plain_text_writes(theirs):
            rlog(logging.INFO, "plain text: normalised characters in a text-file write")
        cwd = working_directory_of(received)
        broken = _validate_tool_calls(mine + theirs, _tool_schemas(payload), cwd)
        broken += reruns
        broken += early_deliverable_writes(theirs, received, cwd)
        if _graph_main_loop(received):
            broken += writes_before_workflow(theirs, received, cwd)
        broken += graph.guarded_writes(theirs, received, cwd)
        if broken and corrections < settings.agentaus_correction_rounds:
            corrections += 1
            rlog(logging.WARNING, "%d malformed tool call(s): %s; correcting",
                 len(broken), "; ".join(m for _, m in broken)[:300])
            rlog(logging.INFO, "malformed call(s) as sent: %s",
                 "; ".join(f"{c['name']}({str(c.get('arguments'))[:200]})" for c, _ in broken))
            payload = _with_tool_results(
                payload, [c for c, _ in broken], [m for _, m in broken])
            pending = []
            continue
        if broken:
            rlog(logging.WARNING, "forwarding %d malformed tool call(s) - out of rounds",
                 len(broken))
        _guard_imports(theirs, received, cwd)
        if mine and tool_round < settings.agentaus_tool_rounds:
            tool_round += 1
            rlog(logging.INFO, "running %d bridge tool call(s): %s",
                 len(mine), ", ".join(c["name"] for c in mine))
            yield progress("running " + ", ".join(c["name"] for c in mine))
            box = {}
            async for beat in _waiting(_run_bridge_tools(
                    client, payload, mine, working_directory_of(original), ran), builder, box):
                yield beat
            payload = box["value"]
            # Whatever the model said on its way to calling the tool is superseded by
            # the answer it is about to give with the result in hand, so it is dropped
            # rather than shown - otherwise the user reads "let me search..." followed
            # by a search they never saw happen.
            if pending:
                rlog(logging.DEBUG, "discarded %d chars of interim narration",
                     len("".join(pending)))
            pending = []
            continue
        # A turn that used no tools and claims it has none is not an answer. Whether it
        # IS that is a judgement about language, so Agentaus makes it - behind a
        # structural gate, so the call only happens when there is something to judge.
        answer_so_far = "".join(pending)
        is_refusal = False
        turn_verdict = "answer"
        if (
            corrections < settings.agentaus_correction_rounds
            # Started in the background and said so: the right reply, not a stall.
            and not awaiting_background(original)
            and could_be_a_refusal(
                answer_so_far,
                tools_offered=bool(known),
                called_a_tool=bool(mine or theirs),
            )
        ):
            async def classify() -> str:
                async with hold("refusal check", "urgent"):
                    return await _agentaus_summarise(
                        client, CLASSIFY_REFUSAL_INSTRUCTION.format(
                            request=_original_request(received)[:4000],
                            ran=ledger.render(original.get("messages") or [], limit=40)
                            or "(nothing yet)",
                            answer=_head_and_tail(answer_so_far, 2500, 1500))
                    )

            try:
                box = {}
                async for beat in _waiting(classify(), builder, box):
                    yield beat
                turn_verdict = read_turn_verdict(box["value"])
                is_refusal = turn_verdict == "refusal"
            except Exception as exc:
                # Cannot classify it, so forward it. Re-asking a good answer is worse
                # than letting a bad one through: the user can repeat a turn.
                rlog(logging.WARNING, "refusal check failed (%s); forwarding the answer",
                     exc)

        if turn_verdict == "stalled" and not _may_reask_stalled(original):
            rlog(logging.WARNING, "judged stalled again, but this conversation has used "
                 "its %d re-asks; forwarding the reply", _STALL_REASK_LIMIT)
            turn_verdict = "answer"
        if is_refusal or turn_verdict == "stalled":
            corrections += 1
            if is_refusal:
                rlog(logging.WARNING,
                     "model refused to use tools it has; correcting and re-asking")
            else:
                rlog(logging.WARNING,
                     "model announced a step and stopped; asking it to take the step")
            yield progress("re-asking: the reply " + (
                "declined to use its tools" if is_refusal else "announced a step it did not take"))
            correction = (REFUSAL_CORRECTION if is_refusal else STALLED_CORRECTION).format()
            payload = {**payload, "messages": list(payload.get("messages") or []) + [
                {"role": "assistant", "content": "".join(pending)[:2000]},
                {"role": "user", "content": correction},
            ]}
            if not is_refusal and payload.get("tools"):
                # Words alone kept failing: "We will perform the edits." ended two runs
                # after both re-asks were spent on more of the same. `required` makes the
                # call the step itself - honoured about one time in three when measured,
                # and harmless when ignored. For this one call only.
                forced_from = payload.get("tool_choice", "(none)")
                payload = {**payload, "tool_choice": "required"}
            pending = []
            continue

        if mine:
            # Out of rounds. These must not be emitted: Claude Code has never heard of
            # `agentaus_search` and would fail the tool_use rather than run it.
            rlog(logging.WARNING,
                 "tool round limit (%d) reached; dropping %d unanswered bridge call(s)",
                 settings.agentaus_tool_rounds, len(mine))
            if not pending and not theirs:
                # Every one of those searches is still in the payload. Asking again with
                # the tools REMOVED forces the model to answer from what it gathered,
                # which is strictly better than apologising for having gathered it.
                # Observed: a locatable symbol spent twelve rounds being searched for and
                # the turn ended "I ran out of search rounds", with the answer sitting in
                # the conversation the whole time.
                box = {}
                async for beat in _waiting(_answer_without_tools(client, payload), builder, box):
                    yield beat
                forced = box["value"]
                pending = [forced] if forced else [
                    "I searched without reaching an answer. Narrow the question and I "
                    "will try again."
                ]
        break

    # Same repair as the buffered path: a non-breaking hyphen in a filename looks
    # exactly like a hyphen and cannot be copied, clicked or searched for.
    answer = normalise_for_display("".join(pending))
    if buffering and not answer and not theirs and not accumulator.pending():
        # Agentaus sometimes answers HTTP 200 with no content at all - observed as
        # "200 in 94.8s in=0 out=0" on a question it had every means to answer. An empty
        # reply is never right, so ask once more before forwarding silence.
        rlog(logging.WARNING, "upstream returned an empty answer; asking once more")
        box = {}
        async for beat in _waiting(_answer_without_tools(client, payload), builder, box):
            yield beat
        retried = box["value"]
        if retried:
            answer = retried
            pending = [answer]

    if (buffering and answer and settings.agentaus_syntax_check and not theirs
            and not is_subagent_turn(original)):
        box = {}
        async for beat in _waiting(_fix_syntax(client, answer), builder, box):
            yield beat
        answer = box["value"]
        pending = [answer]

    if buffering and answer:
        if live_started and not theirs:
            yield progress("checking the answer")
        check = None
        if not theirs and not ran and worth_reviewing_turn(original) and worth_reviewing(
            answer, min_chars=settings.agentaus_review_min_chars
        ):
            check = _self_review(client, _last_user_text(original), answer)
        elif (
            not theirs
            and settings.agentaus_grounding_check
            and _grounding_applies(original, answer, ran)
        ):
            # The turns review has to sit out are the turns where a claim can outrun the
            # evidence. This checks against the tools actually run instead of guessing.
            check = _check_grounding(client, original, answer, ran)
        if check is not None:
            box = {}
            async for beat in _waiting(check, builder, box):
                yield beat
            answer = box["value"]
        yield builder.text(answer)

    for call in theirs:
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
    source: AsyncIterator[bytes],
    interval: float,
    make_ping: Callable[[], bytes],
    deadline: float = 0,
) -> AsyncIterator[bytes]:
    """Emit ping events while `source` is silent.

    Claude Code aborts any stream that sends no bytes for 300 seconds, and Agentaus
    sends nothing at all until the whole completion is ready. Without these pings a
    long generation looks like a dead connection.

    The pings are also why `deadline` exists. They defeat the client's idle timeout by
    design, so a turn wedged inside the bridge would otherwise keep the connection alive,
    and the VS Code spinner turning, forever. Past `deadline` seconds the stream ends
    with an error event the client can show. 0 disables it. Only seconds the upstream could
    serve count (throttle.Health): a turn waiting out an outage is not wedged.

    A turn from _handle_agentaus passes no deadline here: its flight holds it (flight.py).
    """
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()
    started = time.monotonic()
    down_at_start = throttle.down_total()

    def served() -> float:
        return time.monotonic() - started - max(0.0, throttle.down_total() - down_at_start)

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
            wait = interval
            if deadline > 0:
                remaining = deadline - served()
                if remaining <= 0:
                    rlog(logging.WARNING,
                         "turn exceeded BRIDGE_TURN_TIMEOUT (%.0fs); ending it", deadline)
                    yield AnthropicStreamBuilder.error(
                        f"The Agentaus turn ran for more than {deadline:.0f}s without "
                        "finishing, so the bridge ended it (BRIDGE_TURN_TIMEOUT). Retry, "
                        "or narrow the request.", "api_error",
                    )
                    return
                wait = min(interval, remaining)
            done, _ = await asyncio.wait({getter}, timeout=wait)
            if not done:
                if deadline > 0 and served() >= deadline:
                    continue            # the deadline branch above reports it
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

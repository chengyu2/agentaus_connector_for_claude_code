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

from . import tools as bridge_tools
from .augment import (
    ADJUDICATE_INSTRUCTION,
    REVIEW_INSTRUCTION,
    REVISE_INSTRUCTION,
    declared_verdict,
    plan_prompt,
    looks_like_tool_refusal,
    should_think,
    working_directory,
    with_guidance,
    with_plan,
    worth_reviewing,
    worth_reviewing_turn,
    REFUSAL_CORRECTION,
)
from .compact import ConversationCompactor
from .distill import ResultDistiller
from .gate import hold
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


# Gateway timeouts are in _RETRYABLE_STATUS because a busy gateway often succeeds on a
# second try. That is true of a small request and false of a large one: if the origin
# could not finish this prompt in time, it will not finish the identical prompt in time
# either. Replaying it costs the timeout again and changes nothing.
#
# Observed: a zoom condensation 524'd and was replayed four times, roughly two minutes
# apart - eight minutes spent to arrive back where it started, while the caller had a
# perfectly good fallback it could have used immediately.
_CAPACITY_STATUS = {504, 520, 522, 524}


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_body: dict,
    headers: dict,
    retry_capacity: bool = True,
) -> httpx.Response:
    """POST, retrying transient connection faults and gateway status codes.

    Only used for buffered (non-streaming) requests, where replaying costs nothing
    because no bytes have been handed to the client yet.

    `retry_capacity=False` surfaces a gateway timeout immediately instead of replaying
    it. Callers that can make the request smaller - split a chunk, truncate a passage,
    recompact a conversation - want the failure now, not four timeouts later.
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

        if not retry_capacity and response.status_code in _CAPACITY_STATUS:
            await response.aread()
            rlog(logging.WARNING,
                 "HTTP %d on a helper call; surfacing it rather than replaying the same "
                 "prompt", response.status_code)
            return response

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
        "stream": settings.agentaus_stream_helpers,
        "system_prompt_overwrite": True,
    }
    started_at = time.monotonic()

    limit = settings.agentaus_helper_timeout_seconds
    if limit > 0:
        try:
            return await asyncio.wait_for(
                _helper_call(client, payload, text, started_at), timeout=limit
            )
        except asyncio.TimeoutError:
            rlog(logging.WARNING, "helper call exceeded %.0fs and was abandoned", limit)
            raise RuntimeError(f"helper call timed out after {limit:.0f}s")
    return await _helper_call(client, payload, text, started_at)


async def _helper_call(
    client: httpx.AsyncClient, payload: dict, text: str, started_at: float
) -> str:
    """One helper call, streamed or buffered. Bounded by the caller."""
    if payload["stream"]:
        # Streamed rather than buffered. A buffered helper call holds a connection open
        # while the server composes the entire reply, and a fan-out of those is what
        # saturates Agentaus - a batch run once queued ~135 of them and stopped making
        # progress at all. Streaming returns the first bytes as they are generated, so a
        # slow reply stops looking like a dead one and the connection drains sooner.
        try:
            return await _stream_helper(client, payload, text, started_at)
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
    client: httpx.AsyncClient, payload: dict, text: str, started_at: float
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
        async for line in upstream.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
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
    rlog(logging.DEBUG, "helper stream ok in %.1fs (first byte %.1fs, %d chars in)",
         time.monotonic() - started_at, first_byte, len(text))
    return "".join(parts)


async def _self_review(client: httpx.AsyncClient, request_text: str, answer: str) -> str:
    """Have Agentaus critique its own answer and revise it if defects are found.

    Returns the answer to use - the original when the review is clean, or a revision.
    Any failure returns the original: a broken review must never lose a good answer.
    """
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


async def _plan_turn(client: httpx.AsyncClient, body: dict) -> str:
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
            plan = await _agentaus_summarise(
                client, plan_prompt(_last_user_text(body), body)
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


def _partition_tool_calls(calls: list, known: set | None = None) -> tuple[list, list, list]:
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
            resolved = _canonical(name, known | set(bridge_tools.BRIDGE_TOOLS))
            if resolved is None:
                invented.append(call)
                continue
            if resolved != name:
                rlog(logging.INFO, "tool name %r resolved to %r", name, resolved)
                call = {**call, "name": resolved}
                name = resolved
        if name in bridge_tools.BRIDGE_TOOLS:
            mine.append(call)
        else:
            theirs.append(call)
    return mine, theirs, invented


def _correction_for(call: dict, known: set) -> str:
    """What to tell a model that called a tool which does not exist."""
    offered = ", ".join(sorted(known)) or "(none)"
    return (
        f"There is no tool named {call.get('name')!r}. You invented it. "
        f"The tools you actually have are: {offered}. "
        f"Call one of those by its exact name, or answer from what you already know. "
        f"Do not guess at a tool name again."
    )


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


def _call_signature(call: dict) -> str:
    """A stable key for "this exact tool call", for spotting a repeat."""
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": arguments}
    return json.dumps(
        {"n": call.get("name") or "", "a": arguments or {}}, sort_keys=True, default=str
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


# Below this share of the window, a timeout cannot be blamed on the prompt. Recompacting
# a request that occupies a fraction of the context achieves nothing, and the bridge did
# exactly that: eight minutes of refit-and-retry on a 3,642-token payload while the
# upstream was timing out on a six-token probe as well. Above it, shrinking is worth a try.
_REFIT_WORTH_TRYING_ABOVE = 0.25


def _worth_refitting(body: dict, limit: int) -> bool:
    """Whether a timeout could plausibly be about the size of this request.

    Distinguishes "the conversation is too big" from "the upstream is unwell". Both
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

    # Written once and reused across refits: a refit recompacts the same turn, and
    # re-planning it would pay for the same call again to get the same plan.
    plan_holder: dict = {"text": None}

    async def prepare(current: dict, scale: float = 1.0) -> tuple[dict, dict]:
        """Fit to the window, add the guidance and the plan, and build the payload.

        Returns (body, payload). Compaction can take a minute or more on a long
        conversation, which is why the streaming path runs this *inside* the response
        generator rather than before it - see the note there.
        """
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
        ledger_source = list(current.get("messages") or [])

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
        # Offer the bridge's own search and steer Grep towards literal lookups. Done
        # here rather than before compaction so the tool list travels with whatever
        # survived the fit; it costs ~250 tokens the estimate does not see, which is
        # immaterial against a 0.8 threshold.
        offered = []
        if settings.agentaus_search:
            offered.append(bridge_tools.SEARCH_SCHEMA)
        if settings.agentaus_web_search:
            offered.append(bridge_tools.WEB_SEARCH_SCHEMA)
        if settings.agentaus_investigate:
            offered.append(bridge_tools.INVESTIGATE_SCHEMA)
        if settings.agentaus_zoom:
            offered.append(bridge_tools.ZOOM_SCHEMA)
        if offered:
            fitted = inject_bridge_tools(fitted, offered)

        if settings.agentaus_tool_ledger:
            fitted = {**fitted, "system": with_ledger(
                fitted.get("system"), ledger_source,
                limit=settings.agentaus_tool_ledger_limit,
            )}

        # Supplement the system prompt for Agentaus only. Claude turns never reach
        # here - they are forwarded untouched by _passthrough.
        if settings.agentaus_guidance:
            fitted = {**fitted, "system": with_guidance(fitted.get("system"), fitted)}

        if settings.agentaus_thinking and should_think(fitted):
            if plan_holder["text"] is None:
                with _Phase("planning"):
                    plan_holder["text"] = await _plan_turn(client, fitted)
            fitted = {**fitted, "system": with_plan(fitted.get("system"), plan_holder["text"])}

        built = anthropic_request_to_agentaus(
            fitted,
            system_prompt_overwrite=settings.system_prompt_overwrite,
            stream=settings.upstream_stream and wants_stream,
        )
        if settings.log_bodies:
            rlog(logging.INFO, "-> agentaus payload: %s", json.dumps(built)[: settings.log_body_chars])
        return fitted, built

    if not wants_stream:
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
                        json_body={**payload, "stream": False},
                        headers=_agentaus_headers(),
                    )
                except httpx.HTTPError as exc:
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

            known = _known_tool_names(payload)
            mine, _theirs, invented = _partition_tool_calls(_openai_calls(data), known)
            if invented and corrections < settings.agentaus_correction_rounds:
                corrections += 1
                rlog(logging.WARNING, "model invented %d tool name(s): %s; correcting",
                     len(invented), ", ".join(c["name"] for c in invented))
                payload = _with_tool_results(
                    payload, invented, [_correction_for(c, known) for c in invented])
                continue
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
                    client, payload, mine, working_directory(body.get("system")), ran)
                continue
            # Out of rounds with a bridge tool still pending. It must not reach Claude
            # Code, which has never heard of it and would fail the tool_use outright.
            rlog(logging.WARNING,
                 "tool round limit (%d) reached; dropping %d unanswered bridge call(s)",
                 settings.agentaus_tool_rounds, len(mine))
            data = _without_bridge_calls(data)
            break

        # Review only a plain text answer. A turn that calls tools is mid-task, and
        # rewriting it would break the tool_use the client is waiting on.
        choice0 = (data.get("choices") or [{}])[0]
        msg0 = choice0.get("message") or {}
        if (
            settings.agentaus_self_review
            and not msg0.get("tool_calls")
            and worth_reviewing_turn(body)
            and worth_reviewing(msg0.get("content") or "",
                                min_chars=settings.agentaus_review_min_chars)
        ):
            reviewed = await _self_review(client, _last_user_text(body), msg0["content"])
            if reviewed != msg0["content"]:
                msg0 = {**msg0, "content": reviewed}
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
                client, prepared_payload, prepared_body, display_model, started, refit,
                plan=plan_holder["text"],
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
    plan: str | None = None,
) -> AsyncIterator[bytes]:
    """Produce a valid Anthropic SSE stream from an Agentaus response."""
    builder = AnthropicStreamBuilder(
        model,
        input_tokens=estimate_request_tokens(original),
        chunk_chars=settings.chunk_chars,
    )
    yield builder.start()

    # The plan leads the message, where a native thinking block would. Emitted before
    # the upstream call rather than after it, so the user sees the model's reasoning
    # while the answer is still being generated instead of all at once at the end.
    if plan and settings.agentaus_thinking_visible:
        yield builder.thinking(plan)

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

    # Rounds of bridge-executed tool calls. `agentaus_search` is answered here and
    # re-asked upstream; the client never learns the call happened. Bounded, so a
    # model that keeps searching still has to produce an answer eventually.
    tool_round = 0
    corrections = 0
    ran: dict = {}
    while True:
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

        calls = accumulator.drain()
        known = _known_tool_names(payload)
        mine, theirs, invented = _partition_tool_calls(calls, known)
        if invented and corrections < settings.agentaus_correction_rounds:
            # Correct it and ask again rather than failing a tool the client cannot run.
            # Deliberately NOT charged to tool_round: a correction is the bridge fixing
            # its own upstream's mistake, and spending the budget for real work on it
            # is what made a live turn run out before it could answer.
            corrections += 1
            rlog(logging.WARNING, "model invented %d tool name(s): %s; correcting",
                 len(invented), ", ".join(c["name"] for c in invented))
            payload = _with_tool_results(
                payload, invented, [_correction_for(c, known) for c in invented])
            pending = []
            continue
        if invented:
            rlog(logging.WARNING, "dropping %d invented tool call(s) - out of rounds",
                 len(invented))
        if mine and tool_round < settings.agentaus_tool_rounds:
            tool_round += 1
            rlog(logging.INFO, "running %d bridge tool call(s): %s",
                 len(mine), ", ".join(c["name"] for c in mine))
            payload = await _run_bridge_tools(
                client, payload, mine, working_directory(original.get("system")), ran)
            # Whatever the model said on its way to calling the tool is superseded by
            # the answer it is about to give with the result in hand, so it is dropped
            # rather than shown - otherwise the user reads "let me search..." followed
            # by a search they never saw happen.
            if pending:
                rlog(logging.DEBUG, "discarded %d chars of interim narration",
                     len("".join(pending)))
            pending = []
            continue
        # A turn that used no tools and claims it has none is not an answer. Re-ask
        # rather than forwarding it: the tools were on the wire the whole time.
        if (
            not mine and not theirs and known
            and corrections < settings.agentaus_correction_rounds
            and looks_like_tool_refusal("".join(pending))
        ):
            corrections += 1
            rlog(logging.WARNING,
                 "model refused to use tools it has; correcting and re-asking")
            payload = {**payload, "messages": list(payload.get("messages") or []) + [
                {"role": "assistant", "content": "".join(pending)[:2000]},
                {"role": "user", "content": REFUSAL_CORRECTION},
            ]}
            pending = []
            continue

        if mine:
            # Out of rounds. These must not be emitted: Claude Code has never heard of
            # `agentaus_search` and would fail the tool_use rather than run it.
            rlog(logging.WARNING,
                 "tool round limit (%d) reached; dropping %d unanswered bridge call(s)",
                 settings.agentaus_tool_rounds, len(mine))
            if not pending and not theirs:
                pending = ["I ran out of search rounds before finishing. Ask again and "
                           "I will continue from what I found."]
        break

    answer = "".join(pending)
    if buffering and answer:
        if (
            not theirs
            and worth_reviewing_turn(original)
            and worth_reviewing(answer, min_chars=settings.agentaus_review_min_chars)
        ):
            answer = await _self_review(client, _last_user_text(original), answer)
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

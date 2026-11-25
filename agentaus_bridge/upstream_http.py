"""Talking to the Agentaus API: retries, streamed posts,
the context window it reports, and reading its failures."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from typing import AsyncIterator, Callable

import httpx

from . import flight
from . import throttle

from .config import settings
from .request_log import rlog
from .tokens import has_tokeniser as _has_tokeniser
from .translate import ToolCallAccumulator, estimate_request_tokens


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

# The passthrough talks to Anthropic, whose 402 is a billing error - a final answer
# about the client's account, not an outage. Retrying it with backoff would stall the
# turn for nothing, so 402 is explicitly excluded here while the Agentaus policy above
# keeps it deliberately for its own quota outages (the throttle waits those out).
# Everything else - gateway blips, capacity - stays retryable.
_PASSTHROUGH_RETRYABLE_STATUS = _RETRYABLE_STATUS - {402}

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
    backoff = settings.retry_backoff_seconds
    ceiling = settings.retry_max_delay_seconds
    base = backoff * (2 ** min(attempt, 30))
    # Bound the TOTAL at the ceiling while keeping jitter alive at saturation - a
    # fleet of clients retrying a long outage must not fall back into lockstep, which
    # is exactly what capping after the jitter would cause. So one jitter interval is
    # reserved under the ceiling: below saturation the curve is unchanged, at
    # saturation the pause still spreads over the final interval [ceiling - backoff, ceiling].
    # A ceiling tighter than one backoff interval leaves no room to reserve, so there
    # the pause just spreads over [0, ceiling].
    if ceiling <= backoff:
        return random.uniform(0, max(ceiling, 0.0))
    return min(base, ceiling - backoff) + random.uniform(0, backoff)


def _is_rate_limited(why: str) -> bool:
    return why.startswith("HTTP 429")


async def _sleep_before_retry(attempt: int, why: str, attempt_s: float = 0.0) -> None:
    delay = _retry_delay(attempt)
    if _is_rate_limited(why):
        # A 429 (Agentaus or the Anthropic passthrough) waits at most
        # BRIDGE_RATE_LIMIT_MAX_DELAY - a real ceiling, jittered over its upper half so
        # concurrent retries spread out. The ceiling is floored at 1 s so a cap of 0
        # cannot become a zero-delay retry loop.
        ceiling = max(settings.rate_limit_max_delay_seconds, 1.0)
        delay = min(delay, ceiling * random.uniform(0.5, 1.0))
    rlog(logging.WARNING, "upstream attempt %d failed (%s); retrying in %.2fs", attempt + 1, why, delay)
    # Neither the failed attempt nor this wait counts against the turn's timeout: retrying
    # through a slump is waiting, not being wedged (see flight.Flight.served). The attempt
    # is credited for how long it actually took - a gateway failure dies near the 125 s
    # timeout, a refused connection in milliseconds - not for a flat guess.
    flight.note_wait(delay + max(0.0, attempt_s))
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
        attempt_started = time.monotonic()
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
            await _sleep_before_retry(
                attempt, _describe(exc), time.monotonic() - attempt_started)
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
            await _sleep_before_retry(
                attempt, f"HTTP {response.status_code}", time.monotonic() - attempt_started)
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
                    attempt, f"in-band: {str(in_band.get('message'))[:60]}",
                    time.monotonic() - attempt_started)
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
#
# (Without a real tokeniser the estimate itself is only chars/4, so this threshold is
# effectively half the window: _worth_refitting doubles the fallback estimate, because
# an under-count there misclassifies an oversized request as a sick upstream and replays
# it into the same timeout forever.)
_REFIT_WORTH_TRYING_ABOVE = 1.0


def _worth_refitting(body: dict, limit: int) -> bool:
    """Whether a timeout could plausibly be about the size of this request.

    Distinguishes "the conversation does not fit" from "the upstream is unwell". Both
    arrive as HTTP 524 and only the first is something the bridge can act on.

    Without a real tokeniser the estimate is chars/4, which under-counts dense payloads
    by roughly 2x - enough to turn "does not fit the window" into "fits", so the estimate
    is doubled before comparing. Over-counting refits a conversation that would have
    fitted anyway; under-counting never breaks the replay-into-the-same-timeout loop
    this function exists to stop.
    """
    if limit <= 0:
        return False
    estimate = estimate_request_tokens(body)
    if not _has_tokeniser():
        estimate *= 2
    return estimate > limit * _REFIT_WORTH_TRYING_ABOVE


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

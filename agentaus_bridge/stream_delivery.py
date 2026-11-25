"""Keeping a response alive while the bridge waits:
heartbeats, keepalive pings and flight relays."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Callable

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from . import flight
from . import throttle

from .bridge_responses import _error_response
from .config import settings
from .request_log import rlog
from .translate import AnthropicStreamBuilder


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
        name = getattr(aw, "__name__", None) or getattr(
            aw.get_coro() if hasattr(aw, "get_coro") else None, "__name__", "call")
        if name not in ("following", "__aenter__"):
            rlog(logging.INFO, "waited on %s for %.1fs", name, time.monotonic() - started)


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

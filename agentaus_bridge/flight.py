"""One upstream turn per logical request, shared by every identical request.

Measured (d14): a turn held past 600 s was cut by Claude Code, which at once sent the same
body again without streaming (136,301 bytes streamed, 136,302 not - only `stream` differed),
then re-sent that copy nine more times, 300 s apart. The bridge never noticed a buffered client
had gone, so every copy queued for a slot and made up to five upstream attempts: 96 of d14's
100 buffered outcomes came after their client had left, and a stopped round's bridge was still
calling Agentaus an hour later with nobody listening.

So a turn runs as a *flight*, detached from the client that started it:
  - an identical request (the body without `stream` and `max_tokens`, plus the agent id
    header) joins the flight already running - a buffered fallback joins the stream it
    replaces, and gets the same answer assembled into one message;
  - a client that leaves does not stop the turn at once: it keeps running for
    BRIDGE_FLIGHT_GRACE seconds, for the retry to join, and is cancelled then if none has,
    whether or not any other request arrives;
  - a turn that finished after its client had left is kept for a late re-send; a failed turn
    never is, and neither is one every client saw to the end;
  - each turn has the BRIDGE_TURN_TIMEOUT deadline, counted only while the upstream can serve.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import AsyncIterator, Awaitable, Callable

from . import throttle
from .config import settings
from .translate import AnthropicStreamBuilder

log = logging.getLogger("agentaus-bridge")

STREAM, BUFFERED = "stream", "buffered"


def key_of(body: dict, headers=None) -> str:
    """The same request: the body without `stream` and `max_tokens` (the buffered fallback may
    change both), plus the agent id - subagents share a session, and two agents given the same
    prompt must each get their own sample."""
    keep = {k: v for k, v in (body or {}).items() if k not in ("stream", "max_tokens")}
    agent = (headers.get("x-claude-code-agent-id") if headers is not None else None) or ""
    raw = json.dumps([agent, keep], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class Flight:
    def __init__(self, key: str, kind: str) -> None:
        self.key, self.kind = key, kind
        self.events: list[bytes] = []
        self.result: tuple[int, bytes, str] | None = None   # buffered: status, body, media type
        self.done = self.failed = self.abandoned = self.timed_out = False
        self.subscribers = 0
        self.task: asyncio.Task | None = None
        self.cond = asyncio.Condition()
        self.started = time.monotonic()
        self.down_at_start = throttle.down_total()
        self._timer: asyncio.TimerHandle | None = None

    def served(self) -> float:
        """Seconds since the turn started, less the seconds the upstream was down."""
        return time.monotonic() - self.started - max(0.0, throttle.down_total() - self.down_at_start)


_flights: dict[str, Flight] = {}


def reset() -> None:
    """Forget every flight - at startup, so an event loop never inherits another's."""
    _flights.clear()


def cancel_all() -> None:
    for f in list(_flights.values()):
        if f.task is not None and not f.task.done():
            f.task.cancel()
    _flights.clear()


def running() -> int:
    return sum(1 for f in _flights.values() if not f.done)


def find(key: str, kind: str | None = None) -> Flight | None:
    f = _flights.get(key)
    if f is None or f.failed or (kind is not None and f.kind != kind):
        return None
    return f


async def _notify(f: Flight) -> None:
    async with f.cond:
        f.cond.notify_all()


def _finish(f: Flight) -> None:
    f.done = True
    if f._timer is not None:
        f._timer.cancel()
        f._timer = None
    if f.failed or not f.abandoned:
        # A failure is never replayed to a retry; a turn every client saw needs no keeping.
        if _flights.get(f.key) is f:
            _flights.pop(f.key, None)
        return
    loop = asyncio.get_running_loop()
    f._timer = loop.call_later(settings.flight_keep_done_seconds, _drop, f)


def _drop(f: Flight) -> None:
    if _flights.get(f.key) is f:
        _flights.pop(f.key, None)


async def _watch_deadline(f: Flight, deadline: float, on_timeout: Callable[[], None]) -> None:
    """Cancel the turn once it has been served `deadline` seconds. 0 disables."""
    if deadline <= 0:
        return
    while not f.done:
        remaining = deadline - f.served()
        if remaining <= 0:
            f.timed_out = True
            log.warning("turn exceeded BRIDGE_TURN_TIMEOUT (%.0fs served); ending it", deadline)
            on_timeout()
            if f.task is not None:
                f.task.cancel()
            return
        await asyncio.sleep(min(remaining, 15.0))


TIMEOUT_TEXT = ("The Agentaus turn ran for more than {deadline:.0f}s without finishing, so the "
                "bridge ended it (BRIDGE_TURN_TIMEOUT). Retry, or narrow the request.")


def start_stream(key: str, source: AsyncIterator[bytes], deadline: float = 0) -> Flight:
    """Run `source` (SSE bytes) as a flight; subscribers replay its events."""
    f = Flight(key, STREAM)
    _flights[key] = f

    def timed_out() -> None:
        f.events.append(AnthropicStreamBuilder.error(TIMEOUT_TEXT.format(deadline=deadline),
                                                     "api_error"))
        f.failed = True

    async def pump() -> None:
        watcher = asyncio.ensure_future(_watch_deadline(f, deadline, timed_out))
        try:
            async for chunk in source:
                if b"event: error" in chunk:
                    f.failed = True
                f.events.append(chunk)
                await _notify(f)
        except asyncio.CancelledError:
            f.failed = True
            if not f.timed_out:
                log.warning("turn cancelled after %.1fs", time.monotonic() - f.started)
        except Exception as exc:  # surfaced to every subscriber
            log.warning("stream producer error: %s", exc)
            f.failed = True
            f.events.append(AnthropicStreamBuilder.error(str(exc), "api_error"))
        finally:
            watcher.cancel()
            _finish(f)
            await asyncio.shield(_notify(f))

    f.task = asyncio.create_task(pump())
    return f


def start_buffered(key: str, run: Callable[[], Awaitable], deadline: float = 0) -> Flight:
    """Run `run()` - which returns a Starlette Response - as a flight; waiters share it."""
    f = Flight(key, BUFFERED)
    _flights[key] = f

    def timed_out() -> None:
        f.result = _error(504, "api_error", TIMEOUT_TEXT.format(deadline=deadline))
        f.failed = True

    async def pump() -> None:
        watcher = asyncio.ensure_future(_watch_deadline(f, deadline, timed_out))
        try:
            response = await run()
            f.result = (response.status_code, bytes(response.body), response.media_type
                        or "application/json")
            f.failed = response.status_code >= 400
        except asyncio.CancelledError:
            f.failed = True
            if f.result is None:
                f.result = _error(499, "api_error", "the turn was cancelled")
        except Exception as exc:
            log.warning("buffered turn failed: %s", exc)
            f.failed = True
            f.result = _error(502, "api_error", f"Agentaus request failed: {exc}")
        finally:
            watcher.cancel()
            _finish(f)
            await asyncio.shield(_notify(f))

    f.task = asyncio.create_task(pump())
    return f


def _error(status: int, kind: str, message: str) -> tuple[int, bytes, str]:
    return status, json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode(), \
        "application/json"


def _attach(f: Flight) -> None:
    f.subscribers += 1
    if f._timer is not None and not f.done:
        f._timer.cancel()
        f._timer = None


def _detach(f: Flight) -> None:
    f.subscribers -= 1
    if f.done:
        return
    f.abandoned = True
    if f.subscribers > 0:
        return
    grace = settings.flight_grace_seconds
    log.info("no client is left on a turn %.1fs in; it keeps running %.0fs for a retry to join",
             time.monotonic() - f.started, grace)
    loop = asyncio.get_running_loop()
    if f._timer is not None:
        f._timer.cancel()
    f._timer = loop.call_later(grace, _expire, f)


def _expire(f: Flight) -> None:
    f._timer = None
    if f.done or f.subscribers > 0:
        return
    log.warning("no client came back within %.0fs; cancelling the turn",
                settings.flight_grace_seconds)
    _drop(f)
    if f.task is not None:
        f.task.cancel()


async def subscribe(f: Flight) -> AsyncIterator[bytes]:
    """Every event of a stream flight from the start, then the live tail, to the end."""
    _attach(f)
    i = 0
    try:
        while True:
            async with f.cond:
                await f.cond.wait_for(lambda: len(f.events) > i or f.done)
                fresh, finished = f.events[i:], f.done
            i += len(fresh)
            for chunk in fresh:
                yield chunk
            if finished and i >= len(f.events):
                return
    finally:
        _detach(f)


async def _until(f: Flight, ready: Callable[[], bool], timeout: float,
                 gone: Callable[[], Awaitable[bool]] | None) -> bool:
    """Wait until `ready()`, at most `timeout` seconds (0: no limit), polling `gone()` - the
    client's disconnect - every 2 s. True when ready."""
    stop = time.monotonic() + timeout if timeout > 0 else None
    while not ready():
        wait = 2.0 if stop is None else min(2.0, stop - time.monotonic())
        if wait <= 0:
            return False
        try:
            async with f.cond:
                await asyncio.wait_for(f.cond.wait_for(ready), wait)
        except asyncio.TimeoutError:
            pass
        if not ready() and gone is not None and await gone():
            log.warning("buffered client disconnected after %.1fs", time.monotonic() - f.started)
            return False
    return True


async def wait_buffered(f: Flight, timeout: float,
                        gone: Callable[[], Awaitable[bool]] | None = None):
    """A buffered flight's (status, body, media type), or None if the client left or the
    wait timed out first."""
    _attach(f)
    try:
        if not await _until(f, lambda: f.done, timeout, gone):
            return None
        return f.result
    finally:
        _detach(f)


async def collect_stream(f: Flight, timeout: float,
                         gone: Callable[[], Awaitable[bool]] | None = None) -> list | None:
    """Every event of a stream flight once it is done, or None if the client left or the wait
    timed out first."""
    _attach(f)
    try:
        if not await _until(f, lambda: f.done, timeout, gone):
            return None
        return list(f.events)
    finally:
        _detach(f)


_STATUS_FOR = {"invalid_request_error": 400, "authentication_error": 401,
               "permission_error": 403, "not_found_error": 404, "rate_limit_error": 429,
               "overloaded_error": 529}


def assemble(events: list) -> tuple[int, dict]:
    """A stream's SSE events as the one Message a buffered client expects: (status, JSON).

    Thinking blocks - the live draft and the heartbeat - are left out: they are display, and a
    buffered client never saw them stream. An error event becomes an error response."""
    message, blocks, partial = None, {}, {}
    for chunk in events:
        for frame in chunk.decode("utf-8", "replace").split("\n\n"):
            data = next((line[5:].strip() for line in frame.split("\n")
                         if line.startswith("data:")), None)
            if not data:
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "message_start":
                message = dict(event.get("message") or {})
                message["content"] = []
            elif kind == "content_block_start":
                blocks[event["index"]] = dict(event.get("content_block") or {})
                partial[event["index"]] = ""
            elif kind == "content_block_delta" and event.get("index") in blocks:
                delta, block = event.get("delta") or {}, blocks[event["index"]]
                if delta.get("type") == "text_delta":
                    block["text"] = block.get("text", "") + delta.get("text", "")
                elif delta.get("type") == "input_json_delta":
                    partial[event["index"]] += delta.get("partial_json", "")
            elif kind == "content_block_stop" and event.get("index") in blocks:
                block = blocks[event["index"]]
                if block.get("type") == "tool_use":
                    try:
                        block["input"] = json.loads(partial[event["index"]] or "{}")
                    except ValueError:
                        block["input"] = {}
            elif kind == "message_delta" and message is not None:
                message.update({k: v for k, v in (event.get("delta") or {}).items()})
                message["usage"] = {**(message.get("usage") or {}), **(event.get("usage") or {})}
            elif kind == "error":
                error = event.get("error") or {}
                return _STATUS_FOR.get(error.get("type"), 502), {"type": "error", "error": error}
    if message is None:
        return 502, {"type": "error", "error": {"type": "api_error",
                                                "message": "the turn ended before it started"}}
    message["content"] = [b for _, b in sorted(blocks.items())
                          if b.get("type") not in ("thinking", "redacted_thinking")]
    return 200, message

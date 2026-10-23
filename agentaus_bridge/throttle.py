"""One adaptive limit on every call to Agentaus, whoever makes it.

`gate.py` caps the calls the bridge makes on its own and leaves the user's turn alone. That
is the right shape while the upstream is healthy, and the wrong one when it is not: an
ultracode run is many Claude Code sessions at once - a main loop and up to a dozen graph
agents - each of which is "the user's turn" to the bridge, so nothing bounded how hard they
hit Agentaus together.

Measured: past about eight concurrent sessions Agentaus's Cloudflare front returns 524 after
125 s, and a run that kept sending at full width got nothing but 524s for two hours - 1,200
failed calls, no successful one - while every agent dutifully retried.

So the limit adapts, the way TCP does. It starts at the ceiling (AGENTAUS_UPSTREAM_CONCURRENCY,
default 10). A 5xx, 429 or transport failure halves it, never below AGENTAUS_UPSTREAM_FLOOR
(default 2). From there each run of successes at the current level - as many successes as the
level itself, and at least AGENTAUS_UPSTREAM_RAMP_SECONDS since the last change - raises it by
one, back up to the ceiling.

A failure that says something about one request rather than about the upstream does not narrow
it: a "time is up, report now" turn (see graph.time_up), and a retry of a request that already
got a gateway failure. Measured (d10-d14): time-up turns got a 524 20-50% of the time, ordinary
turns about 0.1%; in d14 every 524 at limit 1 was a copy of a time-up turn while ordinary
streams succeeded beside them - the collapse fed on the bridge's own doomed requests. The
caller says which kind a request is through REQUEST_KIND.

It is applied at the transport, beneath every caller, so a main turn, a graph agent and a
summariser chunk all queue for the same slots. A streamed response holds its slot until the
stream is closed, because that is how long the upstream is busy with it.

`Health` keeps the last ten minutes of outcomes for /healthz/upstream, and the seconds the
upstream has been down, which the agents' and turns' clocks leave out.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections import deque

import httpx

from .config import settings

log = logging.getLogger("agentaus-bridge")

# A gateway or upstream failure - the kind that says "send less", not "your request is wrong".
_OVERLOAD = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
# Quota: nothing will succeed until it is topped up (d9: 66 x 402 over five hours).
_QUOTA = 402

# What the request being sent is: "normal", "time_up" (a graph agent told to report now) or
# "retry" (a request that already got a gateway failure). Only a "normal" failure narrows the
# limit or counts against the upstream's health. Set by the caller just before each attempt.
REQUEST_KIND: contextvars.ContextVar[str] = contextvars.ContextVar("request_kind", default="normal")

# Set by the caller's stall watcher (server._watching_for_stall): when the request got its
# slot. Measured (d14): "attempt 3 still waiting after 7380s - upstream has not responded" was
# queue time - the read timeout is 300 s - so the watcher must be able to tell the two apart.
WAIT_STATE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("wait_state", default=None)


class AdaptiveLimit:
    """A semaphore whose size halves on failure and climbs back one step at a time."""

    def __init__(self, ceiling: int, ramp_seconds: float, clock=time.monotonic,
                 floor: int = 2) -> None:
        self.ceiling = max(1, ceiling)
        self.floor = max(1, min(floor, self.ceiling))
        self.limit = self.ceiling
        self.ramp_seconds = max(0.0, ramp_seconds)
        self._clock = clock
        self._changed = clock()
        self._streak = 0
        self._busy = 0
        self._cond: asyncio.Condition | None = None
        self._loop = None

    def _condition(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:
            self._cond, self._loop = asyncio.Condition(), loop
        return self._cond

    @property
    def busy(self) -> int:
        return self._busy

    async def acquire(self) -> None:
        cond = self._condition()
        async with cond:
            await cond.wait_for(lambda: self._busy < self.limit)
            self._busy += 1

    async def release(self, ok: bool, kind: str = "normal") -> None:
        cond = self._condition()
        async with cond:
            self._busy = max(0, self._busy - 1)
            self._record(ok, kind)
            cond.notify_all()

    def _record(self, ok: bool, kind: str = "normal") -> None:
        now = self._clock()
        if not ok:
            if kind != "normal":
                # Evidence about this request, not about the upstream. See REQUEST_KIND.
                log.info("upstream failure on a %s request; the limit stays at %d",
                         kind.replace("_", "-"), self.limit)
                return
            narrowed = max(self.floor, self.limit // 2)
            if narrowed != self.limit:
                log.warning("upstream overloaded: %d concurrent call(s) -> %d (%s)",
                            self.limit, narrowed, health().by_width_line())
            self.limit, self._streak, self._changed = narrowed, 0, now
            return
        self._streak += 1
        if (self.limit < self.ceiling and self._streak >= self.limit
                and now - self._changed >= self.ramp_seconds):
            self.limit += 1
            self._streak, self._changed = 0, now
            log.info("upstream recovering: %d concurrent call(s) (%s)", self.limit,
                     health().by_width_line())


_limit: AdaptiveLimit | None = None


def limit() -> AdaptiveLimit:
    global _limit
    if _limit is None:
        _limit = AdaptiveLimit(settings.agentaus_upstream_concurrency,
                               settings.agentaus_upstream_ramp_seconds,
                               floor=settings.agentaus_upstream_floor)
    return _limit


def reset() -> None:
    """Forget the limit and the health record. For tests, which would otherwise share them."""
    global _limit, _health
    _limit = None
    _health = None


# --- Upstream health ----------------------------------------------------------------------

_GATEWAY = _OVERLOAD | {408, 425, 525, 526, 527}


class Health:
    """The last WINDOW_S of upstream outcomes, and whether the upstream is down.

    Down: DOWN_AFTER gateway failures in a row on ordinary requests and no success for
    DOWN_QUIET_S, or a 402 (quota). Up again at the first success. `down_total()` is the
    seconds spent down since the bridge started - the clock graph agents' limits and the turn
    deadline leave out, so an outage does not use up the time a turn has to do its work.
    Request-specific failures (see REQUEST_KIND) are kept out of every count.
    """

    WINDOW_S = 600.0
    DOWN_AFTER = 3
    DOWN_QUIET_S = 120.0
    _MAX_EVENTS = 5000

    def __init__(self, clock=time.monotonic, wall=time.time) -> None:
        self._clock, self._wall = clock, wall
        self.events: deque = deque()          # (t, outcome, status, latency_s, width, kind)
        self.fails_in_row = 0
        self.last_ok = clock()
        self.down_since: float | None = None   # monotonic
        self._down_wall: float | None = None
        self._down_total = 0.0
        self.reason = ""

    def record(self, outcome: str, status: int | None, latency_s: float, width: int,
               kind: str = "normal") -> None:
        """outcome: "ok", "fail" (gateway/transport), "quota" or "other" (the request's own
        fault, say a 400 - neither a success nor an upstream failure)."""
        now = self._clock()
        self.events.append((now, outcome, status, latency_s, width, kind))
        self._trim(now)
        if outcome == "ok":
            self.fails_in_row, self.last_ok = 0, now
            if self.down_since is not None:
                log.warning("upstream is back after %.0fs down", now - self.down_since)
                self._down_total += now - self.down_since
                self.down_since, self._down_wall, self.reason = None, None, ""
            return
        if outcome == "quota":
            self._enter_down(now, "quota (HTTP 402)")
            return
        if outcome != "fail" or kind != "normal":
            return
        self.fails_in_row += 1
        if self.fails_in_row >= self.DOWN_AFTER and now - self.last_ok >= self.DOWN_QUIET_S:
            self._enter_down(now, f"{self.fails_in_row} gateway failures in a row, no success "
                                  f"for {now - self.last_ok:.0f}s")

    def _enter_down(self, now: float, why: str) -> None:
        if self.down_since is None:
            log.warning("upstream is down: %s", why)
            self.down_since, self._down_wall, self.reason = now, self._wall(), why

    def _trim(self, now: float) -> None:
        while self.events and (now - self.events[0][0] > self.WINDOW_S
                               or len(self.events) > self._MAX_EVENTS):
            self.events.popleft()

    @property
    def down(self) -> bool:
        return self.down_since is not None

    def down_total(self) -> float:
        extra = self._clock() - self.down_since if self.down_since is not None else 0.0
        return self._down_total + extra

    def _counted(self) -> list:
        self._trim(self._clock())
        return [e for e in self.events if e[1] == "ok" or (e[1] == "fail" and e[5] == "normal")]

    def by_width(self) -> dict:
        """{width: [ok, failed]} over the window: whether failures grow with concurrency."""
        table: dict = {}
        for _, outcome, _, _, width, _ in self._counted():
            row = table.setdefault(int(width), [0, 0])
            row[0 if outcome == "ok" else 1] += 1
        return dict(sorted(table.items()))

    def by_width_line(self) -> str:
        table = self.by_width()
        if not table:
            return "no calls in the last 10 min"
        return "ok by width, 10 min: " + ", ".join(
            f"{w}: {ok}/{ok + bad}" for w, (ok, bad) in table.items())

    def snapshot(self, current_limit: int, ceiling: int, busy: int = 0) -> dict:
        counted = self._counted()
        oks = sorted(e[3] for e in counted if e[1] == "ok")
        fails = sum(1 for e in counted if e[1] == "fail")
        n524 = sum(1 for e in counted if e[2] == 524)
        total = len(oks) + fails
        return {
            "ok_10m": len(oks), "fail_10m": fails, "n524_10m": n524,
            "ok_rate_10m": round(len(oks) / total, 3) if total else 1.0,
            "p50_s_10m": round(oks[len(oks) // 2], 1) if oks else 0.0,
            "limit": current_limit, "ceiling": ceiling, "busy": busy,
            "down": self.down, "down_since": self._down_wall,
            "down_total_s": round(self.down_total(), 1), "reason": self.reason,
            "by_width_10m": {str(w): {"ok": ok, "fail": bad}
                             for w, (ok, bad) in self.by_width().items()},
        }


_health: Health | None = None


def health() -> Health:
    global _health
    if _health is None:
        _health = Health()
    return _health


def down_total() -> float:
    """Seconds the upstream has been down since the bridge started (see Health)."""
    return health().down_total()


class StreamIdle(httpx.ReadTimeout):
    """An Agentaus stream that sent nothing for AGENTAUS_STREAM_IDLE_S. A ReadTimeout, so
    every caller already treats it as a retryable transport failure."""


class _HeldStream(httpx.AsyncByteStream):
    """A response body that gives its slot back when it is closed, once, and that fails a
    read which waits longer than `idle` seconds for the next bytes."""

    def __init__(self, inner, settle, idle: float = 0.0) -> None:
        self._inner = inner
        self._settle = settle
        self._idle = idle

    async def __aiter__(self):
        try:
            if self._idle <= 0:
                async for chunk in self._inner:
                    yield chunk
                return
            it = self._inner.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), self._idle)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    log.warning("upstream stream sent nothing for %gs; dropping it", self._idle)
                    raise StreamIdle(f"no bytes from the upstream for {self._idle:g}s") from None
                yield chunk
        except asyncio.CancelledError:
            # The caller went away: says nothing about the upstream.
            await self._settle("cancelled")
            raise
        except GeneratorExit:
            # The reader stopped by choice - at "[DONE]", say.
            await self._settle("ok")
            raise
        except BaseException:
            await self._settle("fail")
            raise

    async def aclose(self) -> None:
        try:
            if hasattr(self._inner, "aclose"):
                await self._inner.aclose()
        finally:
            await self._settle("ok")


class LimitedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that takes a slot from the adaptive limit for each request, and
    records each outcome in the upstream's health."""

    def __init__(self, inner: httpx.AsyncBaseTransport, adaptive=limit, idle: float | None = None) -> None:
        self._inner = inner
        self._adaptive = adaptive
        self._idle = idle

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gate = self._adaptive()
        kind = REQUEST_KIND.get()
        wait_state = WAIT_STATE.get()
        queued_at = time.monotonic()
        if wait_state is not None:
            wait_state["queued_at"] = queued_at
        await gate.acquire()
        sent_at = time.monotonic()
        width = gate.limit
        if wait_state is not None:
            wait_state["sent_at"], wait_state["queued_s"] = sent_at, sent_at - queued_at
        settled = False
        status: int | None = None

        async def settle(outcome: str) -> None:
            # outcome: "ok", "fail", "cancelled"; a status decides the rest.
            nonlocal settled
            if settled:
                return
            settled = True
            if outcome == "ok" and status == _QUOTA:
                outcome = "quota"
            elif outcome == "ok" and status is not None and status >= 400:
                outcome = "other"
            if outcome != "cancelled":
                health().record(outcome, status, time.monotonic() - sent_at, width, kind)
            await gate.release(outcome not in ("fail",), kind)

        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            # A cancelled caller is not an upstream failure.
            await settle("cancelled" if isinstance(exc, asyncio.CancelledError) else "fail")
            raise
        status = response.status_code
        if response.status_code in _OVERLOAD:
            await settle("fail")
            return response
        if hasattr(response, "_content"):
            # Already read into memory: the upstream is done with it.
            await settle("ok")
            return response
        idle = settings.agentaus_stream_idle_seconds if self._idle is None else self._idle
        response.stream = _HeldStream(response.stream, settle, idle)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()

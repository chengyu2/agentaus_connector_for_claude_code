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
default 10). Any 5xx, 429 or transport failure drops it to one call at a time. From there each
run of successes at the current level - as many successes as the level itself, and at least
AGENTAUS_UPSTREAM_RAMP_SECONDS since the last change - raises it by one, back up to the ceiling.

It is applied at the transport, beneath every caller, so a main turn, a graph agent and a
summariser chunk all queue for the same slots. A streamed response holds its slot until the
stream is closed, because that is how long the upstream is busy with it.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import settings

log = logging.getLogger("agentaus-bridge")

# A gateway or upstream failure - the kind that says "send less", not "your request is wrong".
_OVERLOAD = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}


class AdaptiveLimit:
    """A semaphore whose size falls to one on failure and climbs back one step at a time."""

    def __init__(self, ceiling: int, ramp_seconds: float, clock=time.monotonic) -> None:
        self.ceiling = max(1, ceiling)
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

    async def release(self, ok: bool) -> None:
        cond = self._condition()
        async with cond:
            self._busy = max(0, self._busy - 1)
            self._record(ok)
            cond.notify_all()

    def _record(self, ok: bool) -> None:
        now = self._clock()
        if not ok:
            if self.limit > 1:
                log.warning("upstream overloaded: %d concurrent call(s) -> 1", self.limit)
            self.limit, self._streak, self._changed = 1, 0, now
            return
        self._streak += 1
        if (self.limit < self.ceiling and self._streak >= self.limit
                and now - self._changed >= self.ramp_seconds):
            self.limit += 1
            self._streak, self._changed = 0, now
            log.info("upstream recovering: %d concurrent call(s)", self.limit)


_limit: AdaptiveLimit | None = None


def limit() -> AdaptiveLimit:
    global _limit
    if _limit is None:
        _limit = AdaptiveLimit(settings.agentaus_upstream_concurrency,
                               settings.agentaus_upstream_ramp_seconds)
    return _limit


def reset() -> None:
    """Forget the limit. For tests, which would otherwise share one across cases."""
    global _limit
    _limit = None


class _HeldStream(httpx.AsyncByteStream):
    """A response body that gives its slot back when it is closed, once."""

    def __init__(self, inner, settle) -> None:
        self._inner = inner
        self._settle = settle

    async def __aiter__(self):
        try:
            async for chunk in self._inner:
                yield chunk
        except BaseException:
            await self._settle(False)
            raise

    async def aclose(self) -> None:
        try:
            if hasattr(self._inner, "aclose"):
                await self._inner.aclose()
        finally:
            await self._settle(True)


class LimitedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that takes a slot from the adaptive limit for each request."""

    def __init__(self, inner: httpx.AsyncBaseTransport, adaptive=limit) -> None:
        self._inner = inner
        self._adaptive = adaptive

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gate = self._adaptive()
        await gate.acquire()
        settled = False

        async def settle(ok: bool) -> None:
            nonlocal settled
            if not settled:
                settled = True
                await gate.release(ok)

        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            # A cancelled caller is not an upstream failure.
            await settle(isinstance(exc, asyncio.CancelledError))
            raise
        if response.status_code in _OVERLOAD:
            await settle(False)
            return response
        if hasattr(response, "_content"):
            # Already read into memory: the upstream is done with it.
            await settle(True)
            return response
        response.stream = _HeldStream(response.stream, settle)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()

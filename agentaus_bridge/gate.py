"""One process-wide limit on bridge-initiated Agentaus calls.

The bridge makes Agentaus calls the user never asked for: summarising the head of a
long conversation, checking its own summary for gaps, reviewing an answer, expanding a
search query, and reading each chunk of a haystack. Left unbounded these fan out to
hundreds of concurrent requests against one upstream.

The cap is deliberately **global** rather than per-component. Per-component limits look
tidy and are not: two components each capped at 6 permit 12, and the number that
actually matters - how hard the bridge hits Agentaus - is then a property of which
features happen to be enabled rather than of any setting.

What this must NOT guard is the main turn's own upstream call. That call is the user's
actual request; queueing it behind background work would turn a busy gate into a
visible stall, which is the exact failure the streaming path was restructured to avoid.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import settings

log = logging.getLogger("agentaus-bridge")

_gate: "_PriorityGate | None" = None
_gate_loop: asyncio.AbstractEventLoop | None = None

# Waiting is normal; waiting a long time is worth knowing about, because it is the
# difference between "the model is slow" and "the bridge is queued behind itself".
_SLOW_WAIT_SECONDS = 1.0


def gate() -> asyncio.Semaphore:
    """The shared semaphore, created on first use inside a running loop.

    Not a module-level instance: on Python 3.9 `asyncio.Semaphore()` binds to the
    running loop at construction time, so one built at import would bind to whichever
    loop happened to be current then - or to none at all. This venv is 3.9.6.

    Rebound if the loop changes, which happens between tests: each `asyncio.run()`
    creates and closes its own loop, and a semaphore held over from a closed one
    raises rather than blocking.
    """
    global _gate, _gate_loop
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:  # no current loop - let the caller's own failure surface
        loop = None
    if _gate is None or loop is not _gate_loop or _gate.capacity != max_concurrency():
        _gate = _PriorityGate(max_concurrency())
        _gate_loop = loop
    return _gate


def max_concurrency() -> int:
    """How many bridge-initiated calls may be in flight at once.

    AGENTAUS_SUMMARY_CONCURRENCY is honoured when AGENTAUS_MAX_CONCURRENCY was not set,
    so an existing .env keeps working, but it is no longer the name to use: it predates
    there being anything to run concurrently other than summarisation.
    """
    if settings.max_concurrency_is_explicit:
        return max(1, settings.agentaus_max_concurrency)
    if settings.summary_concurrency_is_explicit:
        log.info(
            "AGENTAUS_SUMMARY_CONCURRENCY=%d is deprecated; it now caps every "
            "bridge-initiated Agentaus call. Prefer AGENTAUS_MAX_CONCURRENCY.",
            settings.agentaus_summary_concurrency,
        )
        return max(1, settings.agentaus_summary_concurrency)
    return max(1, settings.agentaus_max_concurrency)


def reset() -> None:
    """Drop the cached semaphore. For tests, which would otherwise inherit a gate
    bound to a closed loop and sized from a previous case's settings."""
    global _gate, _gate_loop
    _gate, _gate_loop = None, None


class _PriorityGate:
    """Capacity with a queue that is not first-come-first-served.

    Not every bridge call is equally urgent, and a plain semaphore cannot express that.
    A search chunk is inside a tool the model is blocked on - the user is watching a
    cursor. The second pass that checks a summary for gaps, or the review that critiques
    an answer, improves quality and can wait: neither is required for the turn to be
    correct.

    With one FIFO queue a burst of background work delays the calls a user is waiting on,
    which is how a run comes to spend 1236 waits on slots while the thing being waited
    for is a quality pass nobody asked for. Urgent waiters are served first here, and
    background waiters take what is left.
    """

    __slots__ = ("_capacity", "_free", "_waiters", "_sequence")

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._free = capacity
        # (priority, arrival, future) - priority first, then arrival, so equal-priority
        # waiters still queue fairly rather than by chance.
        self._waiters: list = []
        self._sequence = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    async def acquire(self, priority: int) -> None:
        if self._free > 0 and not self._waiters:
            self._free -= 1
            return
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._sequence += 1
        self._waiters.append((priority, self._sequence, future))
        self._waiters.sort(key=lambda row: (row[0], row[1]))
        try:
            await future
        except asyncio.CancelledError:
            # Cancelled while queued: leave no ghost waiter behind, and if a slot was
            # handed to us in the same tick, pass it on rather than leaking capacity.
            self._waiters = [w for w in self._waiters if w[2] is not future]
            if future.done() and not future.cancelled():
                self._release_one()
            raise

    def release(self) -> None:
        self._release_one()

    def _release_one(self) -> None:
        while self._waiters:
            _priority, _seq, future = self._waiters.pop(0)
            if not future.done():
                future.set_result(None)
                return
        self._free = min(self._capacity, self._free + 1)


# Lower number wins. Background work yields to anything a turn is actually waiting on.
URGENT = 0
NORMAL = 1
BACKGROUND = 2

_PRIORITIES = {"urgent": URGENT, "normal": NORMAL, "background": BACKGROUND}


class _Held:
    """Async context manager that acquires the gate and reports a long wait."""

    __slots__ = ("_label", "_priority", "_started")

    def __init__(self, label: str, priority: str) -> None:
        self._label = label
        self._priority = _PRIORITIES.get(priority, NORMAL)
        self._started = 0.0

    async def __aenter__(self) -> "_Held":
        self._started = time.monotonic()
        await gate().acquire(self._priority)
        waited = time.monotonic() - self._started
        if waited >= _SLOW_WAIT_SECONDS:
            log.info(
                "%s waited %.1fs for a slot (cap %d) - the bridge is queued behind "
                "its own background work", self._label, waited, max_concurrency(),
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        gate().release()


def hold(label: str = "helper call", priority: str = "normal") -> _Held:
    """`async with hold("search", "urgent"):` around one bridge-initiated call.

    "urgent"     - a turn is blocked on this; serve it first.
    "normal"     - the default.
    "background" - improves quality but the turn is correct without it, so it yields.
    """
    return _Held(label, priority)

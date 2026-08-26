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

_gate: asyncio.Semaphore | None = None
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
    if _gate is None or loop is not _gate_loop:
        _gate = asyncio.Semaphore(max_concurrency())
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


class _Held:
    """Async context manager that acquires the gate and reports a long wait."""

    __slots__ = ("_label", "_started")

    def __init__(self, label: str) -> None:
        self._label = label
        self._started = 0.0

    async def __aenter__(self) -> "_Held":
        self._started = time.monotonic()
        await gate().acquire()
        waited = time.monotonic() - self._started
        if waited >= _SLOW_WAIT_SECONDS:
            log.info(
                "%s waited %.1fs for a slot (cap %d) - the bridge is queued behind "
                "its own background work", self._label, waited, max_concurrency(),
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        gate().release()


def hold(label: str = "helper call") -> _Held:
    """`async with hold("search"):` around one bridge-initiated Agentaus call."""
    return _Held(label)

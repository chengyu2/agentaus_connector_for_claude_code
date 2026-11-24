"""Plans written beside a turn's first call instead of before it (AGENTAUS_PLAN_ASYNC).

The planning pass used to run before the first model call of every agent turn: 8-57 s
(measured over 30 benchmark runs, mean 24 s) of nothing on screen before the model did
anything, and the plan then reached exactly one call - the first. Every later request of
the turn arrives mid tool loop, where planning is skipped, and the plan was not in the
conversation either: it was shown as a thinking block, which the translator drops.

So the plan is now started alongside the first call and kept here, keyed by the turn. The
first call goes out at once without it; every later request of the same turn that finds it
ready carries it in its system prompt. A plan that is not ready yet is not waited for.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable

log = logging.getLogger("agentaus-bridge")

_MAX_TURNS = 256
_MAX_AGE_SECONDS = 3 * 3600


class _Entry:
    __slots__ = ("task", "text", "shown", "at", "started", "waited", "session")

    def __init__(self, task: asyncio.Task, session: str | None = None) -> None:
        self.task = task
        self.session = session
        self.text: str | None = None
        self.shown = False
        self.at = time.monotonic()
        self.started = self.at
        self.waited = False


_turns: "OrderedDict[str, _Entry]" = OrderedDict()


def turn_key(session: str | None, texts: list) -> str | None:
    """The turn a request belongs to: its session, how many things the user has said, and
    the last of them. A tool round trip keeps all three; a new user message changes them.
    None when there is nothing to key on - including no session: two sessionless clients
    that open with the same message would otherwise share one plan, so such a request
    takes the blocking planner instead, which plans each request for itself."""
    if not texts or not session:
        return None
    raw = "\x00".join([session, str(len(texts)), texts[0][:4000], texts[-1][:4000]])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _prune() -> None:
    now = time.monotonic()
    for key in [k for k, e in _turns.items() if now - e.at > _MAX_AGE_SECONDS]:
        _turns.pop(key, None)
    while len(_turns) > _MAX_TURNS:
        _, old = _turns.popitem(last=False)
        if not old.task.done():
            old.task.cancel()


def start(key: str, make: Callable[[], Awaitable[str]], session: str | None = None) -> bool:
    """Start planning the turn `key` in the background, unless it already is. Returns
    whether a new plan was started (a client re-send of the same request reuses one).

    A new turn in `session` cancels that session's plans still being written: the user has
    moved on, nothing will ask for them again, and each holds a gate slot and an upstream
    call that the new turn's own requests are now queued behind. Only a main loop's new
    message starts a plan (never a subagent or a tool round trip), so this cannot cancel a
    plan its own turn is still waiting for."""
    if key in _turns:
        return False
    if session:
        for old_key in [k for k, e in _turns.items()
                        if e.session == session and not e.task.done()]:
            _turns.pop(old_key).task.cancel()
            log.info("a new turn started; cancelled the previous turn's unfinished plan")
    entry: _Entry

    async def run() -> str:
        try:
            text = (await make()) or ""
        except Exception as exc:  # a broken planner must never cost the turn
            log.warning("background planning failed (%s); the turn goes on unplanned", exc)
            text = ""
        entry.text = text.strip()
        log.info("background plan ready after %.1fs (%d chars)",
                 time.monotonic() - entry.started, len(entry.text))
        return entry.text

    entry = _Entry(asyncio.get_running_loop().create_task(run()), session)
    _turns[key] = entry
    _prune()
    return True


def ready(key: str | None) -> tuple[str, bool] | None:
    """(plan, first_time) when the turn's plan is written; None while it is still being
    written, when it failed, or when the turn was never planned. `first_time` is True once,
    so the caller can show the plan to the user the first time it is used."""
    entry = _turns.get(key) if key else None
    if entry is None or not entry.task.done() or not entry.text:
        return None
    entry.at = time.monotonic()
    first = not entry.shown
    entry.shown = True
    return entry.text, first


async def wait_once(key: str | None, timeout: float) -> float:
    """Wait up to `timeout` seconds for the turn's plan, the first time a request asks; later
    requests of the turn never wait. Returns the seconds waited (0 when it did not wait).

    Measured: a turn whose first action is its decisive edit - read the file, then edit it -
    took that edit before the plan arrived, and repeated the very slip the plan prevents (a
    renamed parameter still used under its old name in the body). The first request after a
    tool result is where the plan is worth waiting for: the planner has already had the
    first call's time to run, so what is left is usually short."""
    entry = _turns.get(key) if key else None
    if entry is None or entry.task.done() or entry.waited or timeout <= 0:
        return 0.0
    entry.waited = True
    began = time.monotonic()
    await asyncio.wait({entry.task}, timeout=timeout)
    return time.monotonic() - began


def pending(key: str | None) -> bool:
    entry = _turns.get(key) if key else None
    return entry is not None and not entry.task.done()


def clear() -> None:
    """Tests only."""
    for entry in _turns.values():
        if not entry.task.done():
            entry.task.cancel()
    _turns.clear()

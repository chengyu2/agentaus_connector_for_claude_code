"""Which model each Claude Code session's main loop is on, so its subagents can follow it.

A subagent started from an Agentaus session did not run on Agentaus. Claude Code cannot
"inherit" a custom model id for a subagent, so it asks for its default Claude model
instead - measured on 2.1.281: the main loop on `agentaus`, the Explore subagent it
started on `claude-opus-5-5`, forwarded to Anthropic. The work, and the user's Claude
quota, went to the model the user had switched away from, and nothing said so.

The client marks a subagent's requests: an `x-claude-code-agent-id` header, and
`cc_is_subagent=true` in the billing line of its system prompt. Every request also
carries `x-claude-code-session-id`, shared by a session and its subagents. So the bridge
remembers which model each session's main loop used most recently and sends that
session's subagents the same way. Switch the session to Opus and its subagents go to
Anthropic again, exactly as before - the toggle is untouched.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

# Bounded and aged: the bridge is long-lived, and a session nobody has used for half a
# day is not one whose subagents are about to arrive.
_MAX_SESSIONS = 512
_MAX_AGE_SECONDS = 12 * 3600

_lock = threading.Lock()
_main_loop: "OrderedDict[str, tuple[bool, float]]" = OrderedDict()


def session_of(headers) -> str | None:
    value = headers.get("x-claude-code-session-id") if headers is not None else None
    return value or None


def is_subagent(headers, body: dict) -> bool:
    """Whether this request comes from a subagent rather than a session's main loop."""
    if headers is not None and headers.get("x-claude-code-agent-id"):
        return True
    system = (body or {}).get("system")
    if isinstance(system, list) and system:
        first = system[0]
        text = first.get("text", "") if isinstance(first, dict) else ""
    else:
        text = system if isinstance(system, str) else ""
    return "cc_is_subagent=true" in text[:400]


def note_main_loop(session: str | None, on_agentaus: bool) -> None:
    """Record the model a session's main loop just used."""
    if not session:
        return
    with _lock:
        _main_loop[session] = (on_agentaus, time.monotonic())
        _main_loop.move_to_end(session)
        while len(_main_loop) > _MAX_SESSIONS:
            _main_loop.popitem(last=False)


def main_loop_on_agentaus(session: str | None) -> bool | None:
    """True or False once the session's main loop has been seen; None before that."""
    if not session:
        return None
    with _lock:
        seen = _main_loop.get(session)
    if seen is None or time.monotonic() - seen[1] > _MAX_AGE_SECONDS:
        return None
    return seen[0]


def reset() -> None:
    with _lock:
        _main_loop.clear()

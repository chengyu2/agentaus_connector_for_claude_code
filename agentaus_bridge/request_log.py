"""Request-scoped logging: every line names the request it belongs to."""

from __future__ import annotations

import contextvars
import logging
import time
import uuid


log = logging.getLogger("agentaus-bridge")


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

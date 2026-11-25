"""Responses the bridge writes itself: error bodies, and answers given without calling the model."""

from __future__ import annotations

import logging
import time
import uuid
from typing import AsyncIterator

from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import settings
from .request_log import rlog
from .translate import AnthropicStreamBuilder, estimate_request_tokens


def _exact_command_response(body: dict, exact: tuple, model: str, wants_stream: bool,
                            started: float) -> Response:
    """The turn an exact-command graph agent would have taken, made without the model: the
    Bash call on its first request, the command's output as its reply on the second."""
    kind, value = exact
    if kind == "run":
        arguments = {"command": value, "description": "Run the graph's exact command",
                     "timeout": 600000}
        rlog(logging.INFO, "graph exact command: running it without the model (%s)",
             value.split("\n", 1)[0][:120])
        return _synthetic_response(body, model, wants_stream, started, "exact command",
                                   tool=("Bash", arguments))
    rlog(logging.INFO, "graph exact command: replying with its output (%d chars) without "
         "the model", len(value))
    return _synthetic_response(body, model, wants_stream, started, "exact command", text=value)


def _synthetic_response(body: dict, model: str, wants_stream: bool, started: float, why: str,
                        *, text: str | None = None, tool: tuple | None = None) -> Response:
    """A whole assistant turn made by the bridge - one text block (end_turn) or one tool call
    (tool_use) - streamed or not, as the client asked. No upstream call."""
    call_id = f"toolu_{uuid.uuid4().hex[:24]}"
    if wants_stream:
        builder = AnthropicStreamBuilder(model, input_tokens=estimate_request_tokens(body),
                                         chunk_chars=settings.chunk_chars)

        async def events() -> AsyncIterator[bytes]:
            yield builder.start()
            if tool is not None:
                yield builder.tool_use(call_id, tool[0], tool[1])
                yield builder.finish("tool_calls", None)
            else:
                yield builder.text(text or "")
                yield builder.finish("stop", None)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    content = ([{"type": "tool_use", "id": call_id, "name": tool[0], "input": tool[1]}]
               if tool is not None else [{"type": "text", "text": text or ""}])
    rlog(logging.INFO, "POST /v1/messages model=%s route=agentaus stream=false -> 200 in %.1fs "
         "(%s, no upstream call)", model, time.monotonic() - started, why)
    return JSONResponse({
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
        "model": model, "content": content,
        "stop_reason": "tool_use" if tool is not None else "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": estimate_request_tokens(body), "output_tokens": 0},
    })


# --------------------------------------------------------------------------------------
# Error helpers
# --------------------------------------------------------------------------------------

def _error_type_for_status(status: int) -> str:
    if status in (401, 403):
        return "authentication_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "api_error"
    return "invalid_request_error"


def _error_response(status: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"type": "error", "error": {"type": error_type, "message": message}},
        status_code=status,
    )


def _upstream_error_response(status: int, text: str) -> JSONResponse:
    # Log the body, not just the status. httpx already logs "HTTP/1.1 400 Bad Request"
    # and stops there, so a rejection that is not about prompt length leaves no trace
    # of its cause - which is precisely the case a malformed-request bug presents as.
    rlog(logging.WARNING, "agentaus rejected the request: HTTP %d %s",
         status, (text or "")[:1000])
    return _error_response(
        status, _error_type_for_status(status), f"Agentaus error: {text[:500]}"
    )

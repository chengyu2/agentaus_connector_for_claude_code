"""Translate the bridge's Anthropic-shaped replies into OpenAI chat-completions shape.

The iOS / phone path (`POST /v1/chat/completions`) reuses the bridge's full Agentaus
pipeline - `server._handle_agentaus` - so it gets compaction, retries and live
streaming for free. That pipeline replies in Anthropic /v1/messages format, which no
ordinary chat client understands. This module converts the reply back:

  * buffered  ->  anthropic_message_to_openai : Anthropic message dict -> OpenAI
                 {"choices":[{"message":{...}}]} completion.
  * streamed  ->  openai_chat_stream          : re-emits the Anthropic SSE frame
                 stream as OpenAI `data:` completion chunks + [DONE].

Mobile chat bodies are plain `messages: [{role, content}]` (strings, no tools), and
the Anthropic pipeline hands those through unchanged, so no request conversion is
needed - the same body travels to `agentaus_bridge.translate.anthropic_request_to_agentaus`.
"""

from __future__ import annotations

import json
import time
import uuid


def anthropic_message_to_openai(data: dict, model: str) -> dict:
    """Convert one Anthropic assistant message into an OpenAI chat.completion.

    `data` is the JSON body of the bridge's /v1/messages reply: content is a list of
    blocks, only the ``text`` blocks are the answer a chat client can display.
    """
    content = "\n".join(
        block.get("text", "")
        for block in (data.get("content") or [])
        if isinstance(block, dict)
    )
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    return {
        "id": data.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content if content else None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def openai_error(message: str, status: int,
                error_type: str = "invalid_request_error") -> dict:
    """An OpenAI-style error body for the chat/completions contract."""
    return {"error": {"message": message, "type": error_type, "code": status}}


def _sse_data(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n".encode()


def _split_frames(buf: bytearray) -> list[tuple[str, dict | None]]:
    """Pull complete ``event:\\n...\\ndata: {...}\\n\\n`` frames out of a byte buffer.

    The bridge's SSE frames are terminated by a blank line (``translate.sse``), and a
    yielded chunk may hold several frames or part of one, so the buffer is drained in
    place and any trailing partial frame stays for the next chunk.
    """
    out: list[tuple[str, dict | None]] = []
    while b"\n\n" in buf:
        idx = buf.index(b"\n\n")
        frame = bytes(buf[:idx])
        del buf[: idx + 2]
        event = "message"
        data: dict | None = None
        for line in frame.split(b"\n"):
            if line.startswith(b"event:"):
                event = line[6:].strip().decode("utf-8", "replace") or event
            elif line.startswith(b"data:"):
                raw = line[5:].strip()
                if raw:
                    try:
                        data = json.loads(raw)
                    except (TypeError, ValueError):
                        data = None
        out.append((event, data))
    return out


async def openai_chat_stream(anthropic_iter, model: str, *, created: int | None = None):
    """Re-emit an Anthropic SSE stream as OpenAI ``chat.completion.chunk`` chunks.

    ``anthropic_iter`` is the bridge's StreamingResponse body: an async iterator of
    Anthropic SSE frames (``content_block_delta`` with ``text_delta``, keepalive
    ``ping`` s, a final ``message_stop``). Thinking deltas are dropped - they are a
    draft of the same answer on Claude Code, meaningless here - and the text deltas
    become OpenAI content deltas, ending with ``data: [DONE]``.

    Yields ``bytes`` fit for a ``text/event-stream`` StreamingResponse body.
    """
    created = created if created is not None else int(time.time())
    finished = False
    error_message: str | None = None
    while True:
        try:
            chunk = await anthropic_iter.__anext__()
        except StopAsyncIteration:
            break
        if finished:
            continue
        buf = bytearray()
        buf += chunk
        for event, data in _split_frames(buf):
            if not isinstance(data, dict):
                continue
            etype = data.get("type")
            if etype == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield _sse_data(
                        {"choices": [{"index": 0, "delta": {"content": delta["text"]}}]}
                    )
            elif etype == "error":
                err = data.get("error") or {}
                error_message = str(err.get("message") or err.get("type") or "Agentaus error")
            elif etype == "message_stop":
                finished = True
                break
    if finished and not error_message:
        yield _sse_data(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "created": created,
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "model": model,
                "object": "chat.completion.chunk",
            }
        )
        yield b"data: [DONE]\n\n"
    elif error_message:
        yield _sse_data(
            {"error": {"message": error_message, "type": "api_error", "code": 502}}
        )
        yield b"data: [DONE]\n\n"

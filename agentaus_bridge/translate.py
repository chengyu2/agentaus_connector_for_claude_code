"""Pure translation between the Anthropic Messages API and Agentaus' OpenAI-style API.

Nothing in here does I/O, so every branch is unit-testable (see tests/test_translate.py).

Direction A  Claude Code -> Agentaus:  anthropic_request_to_agentaus()
Direction B  Agentaus -> Claude Code:  agentaus_response_to_anthropic()  (non-stream)
                                       AnthropicStreamBuilder            (stream)
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable

# Anthropic stop_reason values keyed by the OpenAI finish_reason we receive.
_STOP_REASON = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}

# Anthropic tool_choice -> OpenAI tool_choice
def _map_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return "auto"


def _flatten_text(content: Any) -> str:
    """Anthropic content may be a plain string or a list of typed blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    parts.append(_flatten_text(block.get("content")))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return _flatten_text([content])
    return str(content)


def _system_to_text(system: Any) -> str:
    """`system` is a string or a list of blocks; Claude Code always sends the list form."""
    if not system:
        return ""
    return _flatten_text(system)


def _tool_result_payload(block: dict) -> str:
    """OpenAI tool messages carry a string; Anthropic tool_result carries blocks."""
    content = block.get("content")
    text = _flatten_text(content)
    if not text and isinstance(content, list):
        # Non-text results (e.g. an image from a screenshot tool) still need a body.
        text = json.dumps(content)[:8000]
    if block.get("is_error"):
        text = f"[tool error] {text}"
    return text or "(no output)"


def anthropic_request_to_agentaus(
    body: dict,
    *,
    system_prompt_overwrite: bool = True,
    stream: bool = False,
) -> dict:
    """Convert one Anthropic /v1/messages body into an Agentaus chat-completions body."""
    messages: list[dict] = []

    system_text = _system_to_text(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for message in body.get("messages", []) or []:
        role = message.get("role", "user")
        content = message.get("content")

        if isinstance(content, str):
            if content.strip():
                messages.append({"role": role, "content": content})
            continue

        blocks = content if isinstance(content, list) else []
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        pending_tool_results: list[dict] = []

        for block in blocks:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    text_parts.append(block)
                continue
            btype = block.get("type")

            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )
            elif btype == "tool_result":
                pending_tool_results.append(block)
            elif btype in ("image", "document"):
                # Agentaus accepts text only; describe the attachment instead of dropping it.
                label = "image" if btype == "image" else "document"
                text_parts.append(f"[{label} attachment omitted: the Agentaus API accepts text input only]")
            elif btype == "thinking":
                # Reasoning blocks from a previous Claude turn: not replayable upstream.
                continue
            elif btype == "tool_reference":
                continue

        # OpenAI ordering: the assistant's tool_calls, then one tool message per result.
        for result in pending_tool_results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.get("tool_use_id", ""),
                    "content": _tool_result_payload(result),
                }
            )

        joined = "\n".join(part for part in text_parts if part).strip()

        if role == "assistant":
            if joined or tool_calls:
                entry: dict[str, Any] = {"role": "assistant", "content": joined or None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                messages.append(entry)
        else:
            if joined:
                messages.append({"role": "user", "content": joined})

    payload: dict[str, Any] = {"messages": messages, "stream": stream}

    if system_text and system_prompt_overwrite:
        # Without this Agentaus prepends its own persona ahead of Claude Code's
        # agent prompt, which costs ~2.2k tokens and fights the tool instructions.
        payload["system_prompt_overwrite"] = True

    tools = _map_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
        choice = _map_tool_choice(body.get("tool_choice")) or "auto"
        payload["tool_choice"] = choice

    return payload


def _map_tools(tools: Any) -> list[dict]:
    """Anthropic tool defs -> OpenAI function defs, skipping server-side tool stubs."""
    if not isinstance(tools, list):
        return []
    mapped: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        schema = tool.get("input_schema")
        if not name or not isinstance(schema, dict):
            # Anthropic server-side tools (web_search_*, code_execution_*) have no
            # input_schema and cannot be executed by Agentaus.
            continue
        mapped.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", "") or "",
                    "parameters": schema or {"type": "object", "properties": {}},
                },
            }
        )
    return mapped


def _parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"__raw_arguments__": str(raw)}


def estimate_tokens(text: str) -> int:
    """Rough char/4 heuristic; Agentaus exposes no tokenizer endpoint."""
    return max(1, len(text) // 4)


def estimate_request_tokens(body: dict) -> int:
    blob = json.dumps(
        {"s": body.get("system"), "m": body.get("messages"), "t": body.get("tools")},
        default=str,
    )
    return estimate_tokens(blob)


def agentaus_response_to_anthropic(data: dict, *, model: str) -> dict:
    """Convert a non-streaming Agentaus completion into an Anthropic Message."""
    choices = data.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}

    content: list[dict] = []
    text = message.get("content") or message.get("refusal") or ""
    if text:
        content.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "input": _parse_arguments(fn.get("arguments")),
            }
        )

    if not content:
        content.append({"type": "text", "text": ""})

    usage = data.get("usage") or {}
    finish = choice.get("finish_reason") or "stop"

    return {
        "id": data.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _STOP_REASON.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        },
    }


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def chunk_text(text: str, size: int) -> Iterable[str]:
    """Agentaus returns whole paragraphs at once; re-chunk so the TUI paints smoothly."""
    if size <= 0 or len(text) <= size:
        yield text
        return
    for start in range(0, len(text), size):
        yield text[start : start + size]


class AnthropicStreamBuilder:
    """Turns an Agentaus response into a well-formed Anthropic SSE event stream.

    Anthropic's contract is strict about block lifecycle: every content block needs a
    content_block_start, its deltas, and a content_block_stop, with indices matching
    their position in the final message. Claude Code's parser depends on it.
    """

    def __init__(self, model: str, *, input_tokens: int = 0, chunk_chars: int = 60):
        self.model = model
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.input_tokens = input_tokens
        self.chunk_chars = chunk_chars
        self.index = -1
        self.open_block = False
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self._started = False

    def start(self) -> bytes:
        self._started = True
        return sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
                },
            },
        )

    @staticmethod
    def ping() -> bytes:
        return sse("ping", {"type": "ping"})

    def _close_open_block(self) -> bytes:
        if not self.open_block:
            return b""
        self.open_block = False
        return sse("content_block_stop", {"type": "content_block_stop", "index": self.index})

    def text(self, text: str) -> bytes:
        """Emit a text block (opening one if needed) for the given chunk."""
        if not text:
            return b""
        out = b""
        if not self.open_block:
            self.index += 1
            self.open_block = True
            out += sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self.index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        for piece in chunk_text(text, self.chunk_chars):
            out += sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
        self.output_tokens += estimate_tokens(text)
        return out

    def tool_use(self, call_id: str, name: str, arguments: Any) -> bytes:
        """Emit a complete tool_use block: start, input_json_delta, stop."""
        out = self._close_open_block()
        self.index += 1
        arguments_json = (
            arguments if isinstance(arguments, str) else json.dumps(arguments or {})
        )
        # Anthropic requires input:{} at start; the real input arrives as partial JSON.
        out += sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": self.index,
                "content_block": {
                    "type": "tool_use",
                    "id": call_id or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": name,
                    "input": {},
                },
            },
        )
        for piece in chunk_text(arguments_json or "{}", self.chunk_chars):
            out += sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {"type": "input_json_delta", "partial_json": piece},
                },
            )
        out += sse("content_block_stop", {"type": "content_block_stop", "index": self.index})
        self.stop_reason = "tool_use"
        self.output_tokens += estimate_tokens(arguments_json)
        return out

    def finish(self, finish_reason: str | None, usage: dict | None) -> bytes:
        out = self._close_open_block()
        if finish_reason:
            self.stop_reason = _STOP_REASON.get(finish_reason, self.stop_reason)
        usage = usage or {}
        output_tokens = int(
            usage.get("output_tokens") or usage.get("completion_tokens") or self.output_tokens
        )
        out += sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        )
        out += sse("message_stop", {"type": "message_stop"})
        return out

    @staticmethod
    def error(message: str, error_type: str = "api_error") -> bytes:
        return sse("error", {"type": "error", "error": {"type": error_type, "message": message}})


class ToolCallAccumulator:
    """Reassembles OpenAI tool_call deltas that may arrive split across chunks.

    Agentaus currently sends each call complete in one delta, but the OpenAI wire
    format allows fragmenting name/arguments across chunks, so we handle both.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict] = {}

    def add(self, delta_calls: Iterable[dict]) -> None:
        for call in delta_calls or []:
            if not isinstance(call, dict):
                continue
            idx = call.get("index", 0) or 0
            slot = self._calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if call.get("id"):
                slot["id"] = call["id"]
            fn = call.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def drain(self) -> list[dict]:
        ordered = [self._calls[key] for key in sorted(self._calls)]
        self._calls.clear()
        return ordered

    def __bool__(self) -> bool:
        return bool(self._calls)

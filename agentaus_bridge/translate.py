"""Pure translation between the Anthropic Messages API and Agentaus' OpenAI-style API.

Nothing in here does I/O, so every branch is unit-testable (see tests/test_translate.py).

Direction A  Claude Code -> Agentaus:  anthropic_request_to_agentaus()
Direction B  Agentaus -> Claude Code:  agentaus_response_to_anthropic()  (non-stream)
                                       AnthropicStreamBuilder            (stream)
"""

from __future__ import annotations

import base64
import hashlib
import json

from .text import normalise_for_display
from .tokens import calibrator, count_tokens
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


def _tool_result_payload(block: dict, tool_name: str = "") -> str:
    """OpenAI tool messages carry a string; Anthropic tool_result carries blocks.

    The body is wrapped in a tagged envelope naming the tool that produced it. That is
    not decoration. Handed a bare 60,000-character document as a `tool` message,
    Agentaus answered "I am unable to list the headings because the DOCX file hasn't
    been provided - please upload it" roughly half the time, while holding the entire
    file. Tagging the result and saying plainly that it is real output made it stop.

    The same lesson as everywhere else here: this model follows explicit structure far
    more reliably than it follows prose.
    """
    content = block.get("content")
    text = _flatten_text(content)
    if not text and isinstance(content, list):
        # Non-text results (e.g. an image from a screenshot tool) still need a body.
        text = json.dumps(content)[:8000]
    if not text:
        text = "(no output)"

    if block.get("is_error"):
        return (
            f'<tool_error tool="{tool_name or "unknown"}">\n{text}\n</tool_error>\n'
            f"That call failed. Do not treat its output as an answer."
        )
    return (
        f'<tool_result tool="{tool_name or "unknown"}">\n{text}\n</tool_result>\n'
        f"The content above is the real output of your own {tool_name or 'tool'} call. "
        f"It is already here - use it to answer now, and never ask for it to be "
        f"provided, pasted or uploaded."
    )


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

    # tool_use id -> name, so a result can name the call that produced it.
    called: dict = {}
    for message in body.get("messages", []) or []:
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                called[block.get("id") or ""] = block.get("name") or ""

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
                    "content": _tool_result_payload(
                        result, called.get(result.get("tool_use_id") or "", "")
                    ),
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


# What Grep's description becomes on Agentaus turns. The schema is untouched, so when
# the model does call Grep, Claude Code executes it exactly as before - only the advice
# about *when* to reach for it changes.
_GREP_RESTRICTION = (
    "RESTRICTED: use this ONLY for an exact literal string you can already spell - a "
    "known function name, a specific error message, a config key. If the question is "
    "about how something works, where a behaviour lives, what handles a case, or why a "
    "value is set, this is the WRONG TOOL: call `agentaus_search` instead, which reads "
    "the code by meaning and finds the answer even when your words appear nowhere in "
    "it. A regex over code you have not read looks right on the case you tried and is "
    "wrong on the next.\n\nOriginal description follows.\n"
)


_BASH_RESTRICTION = (
    "RESTRICTED: do NOT use this to search or survey. `find`, `ls -R`, `grep -r` and "
    "`cat` over many files all produce more output than this conversation can carry - "
    "the client truncates it to a preview and you answer from a fragment. To find out "
    "what is in a codebase, or which files are relevant, call `agentaus_search`: it "
    "reads by meaning and returns quotes with line numbers. Use Bash for RUNNING "
    "things - tests, builds, git, one command with bounded output.\n\n"
    "Original description follows.\n"
)


def inject_bridge_tools(body: dict, extra_tools: list[dict]) -> dict:
    """Add the bridge's own tools and steer the model away from regex search.

    Returns a new body; the caller's is not mutated. A no-op when the request carries no
    tools at all, because a turn with no tools is not one where search was on offer.
    """
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools or not extra_tools:
        return body

    present = {t.get("name") for t in tools if isinstance(t, dict)}
    rewritten: list[dict] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") == "Grep":
            existing = tool.get("description", "") or ""
            rewritten.append({**tool, "description": f"{_GREP_RESTRICTION}\n{existing}".strip()})
        elif isinstance(tool, dict) and tool.get("name") == "Bash":
            # Same treatment as Grep, and for the same reason: a caveat appended after a
            # paragraph loses to a strong prior. Only the search-shaped uses are
            # restricted - Bash is still how you run a test.
            existing = tool.get("description", "") or ""
            rewritten.append({**tool, "description": f"{_BASH_RESTRICTION}\n{existing}".strip()})
        else:
            rewritten.append(tool)

    rewritten.extend(t for t in extra_tools if t.get("name") not in present)
    return {**body, "tools": rewritten}


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
    """Token count for `text`.

    Uses a real BPE tokeniser where available, corrected by what Agentaus reports for
    requests we have already counted. Characters-over-four under-counts dense code and
    JSON by roughly half, which is the difference between a request that fits and one
    the API rejects.
    """
    return calibrator.adjust(count_tokens(text))


def raw_token_count(text: str) -> int:
    """Uncalibrated count, for comparing against what Agentaus reports."""
    return count_tokens(text)


def estimate_request_tokens(body: dict) -> int:
    blob = json.dumps(
        {"s": body.get("system"), "m": body.get("messages"), "t": body.get("tools")},
        default=str,
    )
    return estimate_tokens(blob)



def _starts_a_clean_turn(message: dict) -> bool:
    """True if the conversation can validly begin at this message.

    A user message carrying `tool_result` blocks refers back to a `tool_use` in the
    preceding assistant message. Beginning there would leave the result orphaned,
    which Agentaus rejects, so such a message can never be the new first one.
    """
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return True


def trim_messages_to_fit(body: dict, limit: int, *, reserve: int = 0) -> tuple[list, int]:
    """Drop the oldest messages until the request fits Agentaus' context window.

    Returns `(messages, dropped)`. `dropped` is 0 when nothing needed removing.

    Oldest-first is what /compact effectively does, and unlike summarising it costs
    no extra model calls - which matters because the bridge is stateless and would
    otherwise re-summarise the whole history on every single turn.

    Dropping continues past the arithmetic threshold until the surviving head is a
    clean user turn, so tool_use/tool_result pairs are never split.

    If even the final message cannot fit, the caller is told (`dropped` covers every
    message removed) and should reject rather than send something malformed.
    """
    messages = list(body.get("messages") or [])
    if limit <= 0 or not messages:
        return messages, 0

    def fits(candidate: list) -> bool:
        probe = {"system": body.get("system"), "messages": candidate, "tools": body.get("tools")}
        return estimate_request_tokens(probe) + reserve <= limit

    if fits(messages):
        return messages, 0

    dropped = 0
    # Always keep the final message: it carries the request being answered.
    while len(messages) > 1 and not fits(messages):
        messages.pop(0)
        dropped += 1
        while len(messages) > 1 and not _starts_a_clean_turn(messages[0]):
            messages.pop(0)
            dropped += 1

    return messages, dropped


def _plan_signature(text: str) -> str:
    """An opaque signature for a synthesised thinking block.

    Anthropic signs thinking blocks so they can be replayed to the API. Nothing replays
    these - the translator drops every thinking block it is handed back - so this only has
    to be present and stable, which is what a renderer written against Anthropic's stream
    shape expects to see.
    """
    digest = hashlib.sha256(text.encode("utf-8", "replace")).digest()
    return "agentaus." + base64.b64encode(digest).decode()


def agentaus_response_to_anthropic(
    data: dict, *, model: str, thinking: str | None = None
) -> dict:
    """Convert a non-streaming Agentaus completion into an Anthropic Message.

    `thinking`, when given, is the plan the bridge asked the model to write before
    answering. It leads the content, as a native thinking block would.
    """
    choices = data.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}

    content: list[dict] = []
    if thinking and thinking.strip():
        content.append({"type": "thinking", "thinking": thinking.strip()})
    # Repaired here because this is the single point every buffered answer passes
    # through on its way to the client.
    text = normalise_for_display(
        message.get("content") or message.get("refusal") or "")
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

    # A message carrying only a thinking block would leave the client with a turn that
    # reasoned and then said nothing, so the empty text block is still required.
    if not any(block["type"] != "thinking" for block in content):
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

    def thinking(self, text: str) -> bytes:
        """Emit a complete thinking block: start, thinking_delta, signature, stop.

        Agentaus has no native thinking mode, so this carries a plan the bridge asked it
        to write as a separate turn. It is genuinely the model's own reasoning about this
        request, just obtained explicitly rather than natively.

        A `signature` IS attached, and that is the fix for a real symptom: a live session
        produced plans - the log shows them generated in 10.3s and 11.8s - and the client
        rendered nothing. Anthropic's own streams always carry a signature_delta, and a
        renderer written against that shape can reasonably discard a block that has none.
        The value is opaque and locally derived; nothing replays these blocks, because
        `anthropic_request_to_agentaus` drops every thinking block it is sent back.

        If a client still shows nothing, AGENTAUS_THINKING_VISIBLE=false emits the plan as
        ordinary tagged text instead, so the reasoning is never simply invisible.
        """
        if not text or not text.strip():
            return b""
        out = self._close_open_block()
        self.index += 1
        out += sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": self.index,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        )
        for piece in chunk_text(text, self.chunk_chars):
            out += sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {"type": "thinking_delta", "thinking": piece},
                },
            )
        out += sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.index,
                "delta": {
                    "type": "signature_delta",
                    "signature": _plan_signature(text),
                },
            },
        )
        out += sse(
            "content_block_stop", {"type": "content_block_stop", "index": self.index}
        )
        self.output_tokens += estimate_tokens(text)
        return out

    def plan_as_text(self, text: str) -> bytes:
        """The fallback: the plan as visible tagged text rather than a thinking block.

        Used when AGENTAUS_THINKING_VISIBLE is off. Tagged rather than prefaced with a
        sentence, so it reads as a distinct section and not as part of the answer.
        """
        if not text or not text.strip():
            return b""
        return self.text(f"<plan>\n{text.strip()}\n</plan>\n\n")

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

    def pending(self) -> bool:
        """Whether any tool call has been accumulated.

        Used to decide whether a turn is prose that may be revised, or a tool call the
        client is waiting on and which must be passed through untouched.
        """
        return bool(self._calls)

    def drain(self) -> list[dict]:
        ordered = [self._calls[key] for key in sorted(self._calls)]
        self._calls.clear()
        return ordered

    def __bool__(self) -> bool:
        return bool(self._calls)

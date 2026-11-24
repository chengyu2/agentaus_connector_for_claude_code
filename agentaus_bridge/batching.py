"""A `run_tools` tool upstream, so Agentaus can ask for several tool calls in one reply.

Why: Agentaus's API returns at most ONE tool call per response, but the model behind it
does write several when the work is independent - measured, "read a.py, b.py and c.py"
costs ~130 completion tokens against ~60 for one read, yet only one call comes back
(usually the last, sometimes none at all: an empty reply). The other calls are generated
and thrown away, and every file read is a full round trip.

What works, measured on a real Claude Code request replayed against the API:
- Offering ONLY a dispatcher (the real tools documented inside it) loses calls: the model
  still calls `Read` by name, and the API silently drops a call to a tool it was not
  offered - 2 of 3 replies came back empty.
- Offering the real tools plus `run_tools`, with a note in the system prompt that only one
  call per reply runs, never lost a call and batched 4 of 4 times mid-task (5-9 reads or
  searches in one reply). Without the note: 2 of 4.

So on the wire (`wrap_payload`): the real tools stay, `run_tools` is added, the note joins
the system prompt, and history is rewritten so an assistant message with several tool
calls reads as the one `run_tools` call it was, with one combined result - the model never
sees a native multi-call that "worked", which would teach it the form the API drops.
`expand_calls` / `expand_response` turn a `run_tools` call in the reply back into the real
calls before anything else in the bridge looks at them; Claude Code then runs the several
tool_use blocks of one assistant message itself (read-only ones concurrently).

The payload the bridge keeps (and reasons about) stays in the native shape; only the copy
sent upstream is changed.
"""

from __future__ import annotations

import json
from typing import Any

DISPATCH = "run_tools"

_DESCRIPTION = (
    "Run SEVERAL of the other functions in one step. Only ONE function call per reply is "
    "executed - any further calls in the same reply are silently discarded. So whenever "
    "you need two or more calls that do not depend on each other's results (reading "
    "several files, several searches, a search plus a read), make ONE call to run_tools "
    "listing them all, instead of separate calls. Each item names the function "
    "(recipient_name) and gives exactly the arguments that function takes (parameters). "
    "Uses run in order. Do not combine a use with one that needs its output: to edit a "
    "file you have not read, or run a script you have not written yet, do that in a "
    "later reply."
)

NOTE = (
    "IMPORTANT - one call per reply: only ONE function call per reply is executed; extra "
    "calls in the same reply are discarded. When you need several independent things "
    "(e.g. read three files, or search and read), call `run_tools` once with all of them "
    "in tool_uses. Use a plain single call when you need just one thing."
)


def dispatcher_tool(tools: list) -> dict:
    """The `run_tools` function def offered beside `tools` (OpenAI function defs)."""
    names = [((t or {}).get("function") or {}).get("name") for t in tools]
    return {
        "type": "function",
        "function": {
            "name": DISPATCH,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_uses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "recipient_name": {
                                    "type": "string",
                                    "enum": [n for n in names if n and n != DISPATCH],
                                },
                                "parameters": {"type": "object"},
                            },
                            "required": ["recipient_name", "parameters"],
                        },
                        "minItems": 2,
                    }
                },
                "required": ["tool_uses"],
            },
        },
    }


def _args_of(call: dict) -> Any:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return raw
    return raw if raw is not None else {}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            (part.get("text") or "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return "" if content is None else str(content)


def _label(index: int, name: str, args: Any) -> str:
    shown = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    if len(shown) > 160:
        shown = shown[:157] + "..."
    return f"[tool_uses[{index}]: {name} {shown}]"


def _collapse_history(messages: list) -> list:
    """Each assistant message's tool calls -> one dispatcher call; their results -> one."""
    out: list = []
    owner: dict = {}          # tool_call_id -> (group id, position in group)
    groups: dict = {}         # group id -> {"calls": [...], "results": {pos: text}, "slot": idx}
    for message in messages:
        role = message.get("role")
        if role == "assistant" and len(message.get("tool_calls") or []) > 1:
            calls = message["tool_calls"]
            gid = calls[0].get("id") or f"call_{len(groups)}"
            uses = []
            for pos, call in enumerate(calls):
                fn = call.get("function") or {}
                args = _args_of(call)
                uses.append({"recipient_name": fn.get("name") or "",
                             "parameters": args if isinstance(args, dict) else {"_raw": args}})
                owner[call.get("id") or ""] = (gid, pos)
            groups[gid] = {"calls": calls, "results": {}, "slot": None}
            out.append({
                **message,
                "tool_calls": [{
                    "id": gid,
                    "type": "function",
                    "function": {"name": DISPATCH,
                                 "arguments": json.dumps({"tool_uses": uses})},
                }],
            })
            continue
        if role == "tool" and message.get("tool_call_id") in owner:
            gid, pos = owner[message["tool_call_id"]]
            group = groups[gid]
            group["results"][pos] = _text_of(message.get("content"))
            if group["slot"] is None:
                group["slot"] = len(out)
                out.append({"role": "tool", "tool_call_id": gid, "content": ""})
            continue
        out.append(message)

    for gid, group in groups.items():
        if group["slot"] is None:
            continue
        calls = group["calls"]
        if len(calls) == 1:
            content = group["results"].get(0, "(no output)")
        else:
            parts = []
            for pos, call in enumerate(calls):
                fn = call.get("function") or {}
                result = group["results"].get(pos, "(no result: this use was not run)")
                parts.append(f"{_label(pos, fn.get('name') or '', _args_of(call))}\n{result}")
            content = "\n\n".join(parts)
        out[group["slot"]] = {"role": "tool", "tool_call_id": gid, "content": content}
    return out


def _with_note(messages: list) -> list:
    if messages and messages[0].get("role") == "system" and isinstance(
            messages[0].get("content"), str):
        return [{**messages[0], "content": messages[0]["content"] + "\n\n" + NOTE}] + messages[1:]
    return [{"role": "system", "content": NOTE}] + messages


def wrap_payload(payload: dict) -> dict:
    """The copy of `payload` to send upstream: `run_tools` added, history to match.

    A no-op for a payload that offers no tools (helper calls strip `tools`): the model is
    not being asked to call anything, and native multi-call history is valid OpenAI.
    """
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return payload
    if any(((t or {}).get("function") or {}).get("name") == DISPATCH for t in tools):
        return payload
    messages = _collapse_history(list(payload.get("messages") or []))
    return {**payload, "tools": list(tools) + [dispatcher_tool(tools)],
            "messages": _with_note(messages)}


def _uses_of(args: Any) -> list | None:
    if isinstance(args, list):
        return args
    if not isinstance(args, dict):
        return None
    for key in ("tool_uses", "calls", "tools", "uses"):
        if isinstance(args.get(key), list):
            return args[key]
    # One use written flat: {"recipient_name": "Read", "parameters": {...}}.
    if any(k in args for k in ("recipient_name", "tool", "name")):
        return [args]
    return None


def _use_name(use: dict) -> str:
    name = use.get("recipient_name") or use.get("tool") or use.get("name") or ""
    name = str(name)
    if name.startswith("functions."):
        name = name[len("functions."):]
    return name


def _use_args(use: dict) -> Any:
    for key in ("parameters", "input", "arguments", "args"):
        if key in use:
            value = use[key]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    return value
            return value
    # Arguments written beside the name instead of under `parameters`.
    return {k: v for k, v in use.items() if k not in ("recipient_name", "tool", "name")}


def expand_calls(calls: list) -> list:
    """Replace each dispatcher call with the real calls it carries (accumulator shape:
    {"id", "name", "arguments"}). Anything else passes through untouched, and a
    dispatcher call that cannot be read is left as it is - the bridge then treats it as
    a call to a tool that does not exist and corrects it."""
    out: list = []
    for call in calls:
        if (call.get("name") or "").replace("-", "_").lower() != DISPATCH:
            out.append(call)
            continue
        raw = call.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            out.append(call)
            continue
        uses = _uses_of(args)
        if not uses:
            out.append(call)
            continue
        base = call.get("id") or "call"
        expanded = []
        for i, use in enumerate(uses):
            if not isinstance(use, dict) or not _use_name(use):
                continue
            use_args = _use_args(use)
            expanded.append({
                "id": base if len(uses) == 1 else f"{base}-u{i}",
                "name": _use_name(use),
                "arguments": use_args if isinstance(use_args, str)
                else json.dumps(use_args if use_args is not None else {}),
            })
        out.extend(expanded if expanded else [call])
    return out


def expand_response(data: dict) -> dict:
    """`expand_calls` applied to a non-streaming chat-completions response."""
    choices = list(data.get("choices") or [])
    if not choices:
        return data
    choice = dict(choices[0] or {})
    message = dict(choice.get("message") or {})
    raw_calls = message.get("tool_calls") or []
    if not any(((c or {}).get("function") or {}).get("name") == DISPATCH for c in raw_calls):
        return data
    flat = [{"id": c.get("id") or "", "name": (c.get("function") or {}).get("name") or "",
             "arguments": (c.get("function") or {}).get("arguments") or "{}"}
            for c in raw_calls]
    message["tool_calls"] = [
        {"id": c["id"], "type": "function",
         "function": {"name": c["name"], "arguments": c["arguments"]}}
        for c in expand_calls(flat)
    ]
    choice["message"] = message
    return {**data, "choices": [choice] + choices[1:]}

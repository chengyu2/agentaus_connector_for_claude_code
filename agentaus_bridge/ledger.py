"""What has already been done this turn, derived from the conversation itself.

`augment.py` names the failure this exists for: Agentaus "re-calls a tool it has already
run, having lost track of what it did". Rule 8 asks it not to, and instruction has not
fixed it - partly because after compaction the earlier calls are genuinely gone. The
model is not forgetting so much as no longer being told.

So the bridge tells it. The ledger is *derived*, never stored: it is a pure function of
the request, computed fresh every turn, which keeps the bridge stateless. And it is
built from the **pre-compaction** message list, which is the entire point - the calls
that fall out of the window are exactly the ones worth remembering.

It costs no Agentaus calls at all.
"""

from __future__ import annotations

import json
from typing import Any

# Fields worth showing for a call, most identifying first. A `Read` is identified by its
# path and a `Bash` by its command; showing the whole input instead would bury that in
# JSON and cost tokens the ledger cannot afford to spend.
_IDENTIFYING = (
    "file_path", "path", "notebook_path", "command", "pattern", "query",
    "url", "prompt", "old_string", "description", "name",
)

_MAX_DIGEST_CHARS = 90


def _digest(value: Any) -> str:
    """The most identifying part of a tool's input, in one short line."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        for key in _IDENTIFYING:
            if value.get(key):
                text = f"{value[key]}"
                break
        else:
            text = json.dumps(value, default=str)
    else:
        text = json.dumps(value, default=str)
    text = " ".join(str(text).split())
    if len(text) > _MAX_DIGEST_CHARS:
        text = text[: _MAX_DIGEST_CHARS - 1] + "…"
    return text


def _outcome(block: dict) -> str:
    """How a tool_result turned out, in one word."""
    if block.get("is_error"):
        return "error"
    content = block.get("content")
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    if not (text or "").strip() or text.strip() == "[]":
        return "empty"
    return "ok"


def collect(messages: list) -> list[tuple[str, str, str]]:
    """Every tool call in the conversation, as (name, digest, outcome).

    Outcome is "pending" for a call whose result has not come back yet - which is the
    normal state of the last call in a turn the model is still working through.
    """
    calls: dict[str, list] = {}
    order: list[str] = []

    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                call_id = block.get("id") or f"anon{len(order)}"
                calls[call_id] = [block.get("name") or "?", _digest(block.get("input")), "pending"]
                order.append(call_id)
            elif block.get("type") == "tool_result":
                call_id = block.get("tool_use_id") or ""
                if call_id in calls:
                    calls[call_id][2] = _outcome(block)

    return [tuple(calls[cid]) for cid in order]


def render(messages: list, *, limit: int = 40) -> str:
    """The ledger as a block for the system prompt, or "" when nothing has run.

    Only the most recent `limit` calls are listed. Older ones are counted rather than
    named: the purpose is to stop a repeat of something recent, and an unbounded list
    would eat the window it is meant to protect.
    """
    entries = collect(messages)
    if not entries:
        return ""

    shown = entries[-limit:]
    dropped = len(entries) - len(shown)

    lines = [f"- {name}({digest}) -> {outcome}" for name, digest, outcome in shown]
    head = (
        f"\n\n[Tools already run in this conversation - {len(entries)} call(s)"
        + (f", {dropped} older not listed" if dropped else "")
        + ". Read the earlier result instead of running one of these again, unless the "
        "inputs have genuinely changed. A call marked `error` or `empty` did not give "
        "you an answer - do not treat it as though it did.]\n"
    )
    return head + "\n".join(lines)


def with_ledger(system, messages: list, *, limit: int = 40):
    """Append the ledger to the system prompt being sent."""
    block = render(messages, limit=limit)
    if not block:
        return system
    if system is None:
        return block.strip()
    if isinstance(system, str):
        return system + block
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": block.strip()}]
    return system

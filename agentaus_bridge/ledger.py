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
    """Append the ledger, and the task list, to the system prompt being sent.

    Both are derived and cost nothing. The ledger says what has been done; the task list
    says what the model itself decided was left. Together they are the cheap half of
    "what next" - the half that does not require another model to guess at it.
    """
    block = render(messages, limit=limit) + render_todos(messages)
    if not block:
        return system
    if system is None:
        return block.strip()
    if isinstance(system, str):
        return system + block
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": block.strip()}]
    return system


# How much of one tool result the grounding check is shown, and how much in total.
# Head and tail both, never the middle: a traceback says what went wrong on its last
# line and a table says what it holds on its first, and the middle of either is padding.
_EVIDENCE_CHARS = 700
_EVIDENCE_BUDGET = 24000


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)
    return json.dumps(content, default=str) if content is not None else ""


def excerpt(text: str, budget: int) -> str:
    text = (text or "").strip()
    if len(text) <= budget:
        return text
    head = budget * 2 // 3
    tail = budget - head
    return f"{text[:head]}\n[...{len(text) - budget} characters omitted...]\n{text[-tail:]}"


def collect_with_output(messages: list) -> list[tuple[str, str, str, str]]:
    """Every tool call as (name, digest, outcome, output) - the output included.

    `collect` deliberately drops what a call returned, because the ledger it feeds
    exists to stop a repeat and has to stay cheap. This is for the one caller that
    needs the opposite.
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
                calls[call_id] = [
                    block.get("name") or "?", _digest(block.get("input")), "pending", ""
                ]
                order.append(call_id)
            elif block.get("type") == "tool_result":
                call_id = block.get("tool_use_id") or ""
                if call_id in calls:
                    calls[call_id][2] = _outcome(block)
                    calls[call_id][3] = _result_text(block)

    return [tuple(calls[cid]) for cid in order]


def render_evidence(
    messages: list,
    *,
    limit: int = 40,
    result_chars: int = _EVIDENCE_CHARS,
    budget: int = _EVIDENCE_BUDGET,
) -> str:
    """What each call returned, for a check that has to judge whether an answer knew it.

    `render` answers "have I already run this?", so it carries no output at all. The
    grounding check asks the opposite question - "could the answer have known this?" -
    and a call rendered as `Bash(python fit.py) -> ok` cannot answer it.

    That gap destroyed correct work. Given a fitted regression and a ledger showing only
    that a script ran, the checker replied *"would need the actual output from fit.py
    showing these values"* and the rewrite turned every correct coefficient into "may be
    0.849; I have not read the source" - the answer contradicting itself while the right
    numbers sat in the file it had just written. The README states the principle this
    broke: a helper pass must never judge what it cannot see.

    Newest calls get the budget first: an answer is usually written from what the turn
    found last, and an older call that no longer fits is still named and counted.
    """
    entries = collect_with_output(messages)
    if not entries:
        return ""

    shown = entries[-limit:]
    dropped = len(entries) - len(shown)

    blocks: list[str] = []
    spent = 0
    for name, digest, outcome, output in reversed(shown):
        head = f"- {name}({digest}) -> {outcome}"
        room = min(result_chars, max(0, budget - spent))
        body = excerpt(output, room) if (output or "").strip() and room else ""
        if body:
            spent += len(body)
            blocks.append(f"{head}\n  <output>\n{body}\n  </output>")
        else:
            blocks.append(head)
    blocks.reverse()

    head = (
        f"\n[{len(entries)} tool call(s) ran"
        + (f", {dropped} older not listed" if dropped else "")
        + ". Each is followed by what it actually returned. A value that appears in an "
        "<output> below IS supported - the agent read it from that result. Long outputs "
        "are excerpted, so a fact consistent with an excerpt is supported too.]\n"
    )
    return head + "\n".join(blocks)


# Statuses TodoWrite uses, and how they read in a reflected list.
_TODO_MARK = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}


def latest_todos(messages: list) -> list[dict]:
    """The most recent TodoWrite list in the conversation, or [].

    Only the last one matters: TodoWrite replaces the whole list on every call, so an
    earlier one is a superseded snapshot, not extra information.
    """
    found: list[dict] = []
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "TodoWrite"
            ):
                payload = block.get("input")
                todos = payload.get("todos") if isinstance(payload, dict) else None
                if isinstance(todos, list):
                    found = [t for t in todos if isinstance(t, dict)]
    return found


def render_todos(messages: list, *, limit: int = 30) -> str:
    """The current task list, reflected back into the system prompt.

    Claude Code re-injects the todo list as a system message after tool use, so the
    model is reminded of the objective instead of being asked to remember it across a
    long loop. Agentaus never saw that: the bridge did not know the tool existed, so on
    an Agentaus turn the list was written once and never referred to again.

    Derived, like the rest of this module - it reads the list the model already wrote
    and costs no upstream call. Nothing here decides what to do next; it restates what
    the model itself said was left, which is the one thing a helper pass can do without
    needing to see what it cannot.
    """
    todos = latest_todos(messages)
    if not todos:
        return ""

    shown = todos[:limit]
    dropped = len(todos) - len(shown)
    lines = []
    for item in shown:
        status = str(item.get("status") or "pending")
        text = " ".join(str(item.get("content") or "").split())[:110]
        lines.append(f"  {_TODO_MARK.get(status, '[ ]')} {text}")

    done = sum(1 for t in todos if t.get("status") == "completed")
    current = next(
        (t for t in todos if t.get("status") == "in_progress"),
        next((t for t in todos if t.get("status") != "completed"), None),
    )
    nxt = " ".join(str((current or {}).get("content") or "").split())[:110]

    tail = (
        f"\nNext: {nxt}\nDo that one thing, then update the list. "
        "Finish the items one at a time rather than attempting several at once, and do "
        "not stop while any remains unfinished."
        if current
        else "\nEvery item is complete. Say so and report the result."
    )
    return (
        f"\n\n[Your task list - {done}/{len(todos)} complete"
        + (f", {dropped} not shown" if dropped else "")
        + ". You wrote this; it is the plan you are working to.]\n"
        + "\n".join(lines)
        + tail
        + "\n"
    )

"""Summarising compaction for conversations that outgrow Agentaus' context window.

Trimming the oldest messages keeps a turn alive but throws the early conversation
away - decisions, file paths, constraints the user stated once and expects to hold.
This module replaces that head of the conversation with a summary instead, so the
detail survives in compressed form.

The hard constraint is that the bridge is **stateless**: Claude Code re-sends the whole
conversation every turn, so a naive implementation would re-summarise the entire history
on every single request - slow, and paid for again each time. The fix is that the
summarised region is a stable *prefix*. Turn N and turn N+1 summarise the same head, so
the result is cached by content hash and computed once, not once per turn.

Layout of a compacted request:

    system prompt + "[earlier conversation summary] ..."     <- compressed head
    <most recent messages, verbatim>                         <- untouched tail

The summary goes in the system prompt rather than in a message so the message sequence
keeps its shape: injecting a synthetic turn risks two user messages in a row and
detaches tool_result blocks from the tool_use they answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from typing import Awaitable, Callable

from .translate import estimate_request_tokens, estimate_tokens

log = logging.getLogger("agentaus-bridge")

# What the summariser is asked to preserve. Written as an extraction task rather than
# "summarise this", because a prose summary of an engineering session reliably drops
# the specifics - paths, identifiers, versions - that later turns depend on.
SUMMARY_INSTRUCTION = """\
You are compacting the earlier part of a software engineering conversation so it can \
continue within a smaller context window. Produce a dense factual record, not prose.

Preserve, exactly as written wherever they appear:
- File paths, function, class and variable names, commands, flags and version numbers
- Decisions that were made, and the reason each one was chosen
- Constraints, preferences and requirements the user stated
- Bugs found, their root cause, and how each was fixed
- What has been completed versus what is still outstanding or was deferred
- Error messages and their resolutions
- Any values that later steps depend on: ports, URLs, identifiers, limits

Rules:
- Never invent detail that is not present. Omission is fine; fabrication is not.
- Keep specifics over generalities: "set BRIDGE_PORT=9100 in .env" beats "changed config".
- Use terse bullet points grouped under short headings.
- Do not add commentary, preamble, or a closing summary.

Conversation to compact:
"""

Summariser = Callable[[str], Awaitable[str]]


def _clean_turn_start(message: dict) -> bool:
    """Whether the conversation may validly begin at this message.

    A user message holding `tool_result` blocks answers a `tool_use` in the assistant
    turn before it. Starting there orphans the result, which Agentaus rejects.
    """
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return True


def split_head_tail(messages: list, keep_tokens: int) -> tuple[list, list]:
    """Split into (head to summarise, tail to keep verbatim).

    The tail grows backwards from the newest message until it reaches `keep_tokens`,
    then extends further until it starts on a clean turn. The newest message is always
    in the tail: it is the one being answered.
    """
    if not messages:
        return [], []

    tail: list = []
    used = 0
    for message in reversed(messages):
        cost = estimate_tokens(json.dumps(message, default=str))
        if tail and used + cost > keep_tokens:
            break
        tail.insert(0, message)
        used += cost

    # Walk the boundary forward until the tail opens on a clean user turn.
    idx = len(messages) - len(tail)
    while idx < len(messages) - 1 and not _clean_turn_start(messages[idx]):
        idx += 1
    return messages[:idx], messages[idx:]


def render_for_summary(messages: list) -> str:
    """Flatten messages into text a model can summarise.

    Tool traffic is included but labelled, because which tool ran and what it returned
    is often the substance of an engineering conversation.
    """
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                lines.append(f"[{role}] {block.get('text', '')}")
            elif kind == "tool_use":
                args = json.dumps(block.get("input") or {}, default=str)[:600]
                lines.append(f"[{role} calls {block.get('name')}] {args}")
            elif kind == "tool_result":
                body = block.get("content")
                text = body if isinstance(body, str) else json.dumps(body, default=str)
                lines.append(f"[tool result] {str(text)[:1500]}")
    return "\n".join(lines)


def _chunk(text: str, budget_tokens: int) -> list[str]:
    """Split text into pieces that each fit a summarisation call, on line boundaries."""
    budget_chars = max(budget_tokens * 4, 2000)
    chunks, current, size = [], [], 0
    for line in text.splitlines():
        line_size = len(line) + 1
        if current and size + line_size > budget_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks


class ConversationCompactor:
    """Summarises the head of a conversation, caching by content hash."""

    def __init__(self, summarise: Summariser, *, cache_size: int = 32) -> None:
        self._summarise = summarise
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_size = cache_size
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(messages: list) -> str:
        return hashlib.sha256(
            json.dumps(messages, default=str, sort_keys=True).encode()
        ).hexdigest()

    def _remember(self, key: str, summary: str) -> None:
        self._cache[key] = summary
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def summarise_head(self, head: list, *, chunk_budget: int) -> str:
        """Summarise `head`, reusing a cached result when the same prefix recurs.

        A head larger than one call can hold is summarised in pieces and the pieces
        summarised again, so arbitrarily long histories reduce in bounded steps.
        """
        key = self._key(head)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self.hits += 1
            return cached

        self.misses += 1
        text = render_for_summary(head)
        pieces = _chunk(text, chunk_budget)

        summaries = []
        for i, piece in enumerate(pieces, 1):
            label = f" (part {i} of {len(pieces)})" if len(pieces) > 1 else ""
            summaries.append(await self._summarise(SUMMARY_INSTRUCTION + label + "\n" + piece))

        summary = "\n".join(s.strip() for s in summaries if s and s.strip())

        # Collapse again if summarising in pieces produced more than will fit.
        rounds = 0
        while estimate_tokens(summary) > chunk_budget and len(summaries) > 1 and rounds < 2:
            summary = (await self._summarise(SUMMARY_INSTRUCTION + "\n" + summary)).strip()
            rounds += 1

        self._remember(key, summary)
        return summary

    async def compact(
        self, body: dict, *, limit: int, reserve: int, keep_fraction: float = 0.5
    ) -> dict:
        """Return a plan describing how to fit `body` into `limit`.

        Keys: `messages`, `summary` (or None), `summarised` (count), `method`.
        `method` is "none", "summarised", or "trimmed" when summarising could not
        make it fit and the head had to be dropped instead.
        """
        messages = list(body.get("messages") or [])
        overhead = estimate_request_tokens(
            {"system": body.get("system"), "messages": [], "tools": body.get("tools")}
        )
        budget = limit - reserve - overhead

        def total(msgs: list, summary: str = "") -> int:
            return estimate_request_tokens(
                {"system": body.get("system"), "messages": msgs, "tools": body.get("tools")}
            ) + estimate_tokens(summary) + reserve

        if total(messages) <= limit:
            return {"messages": messages, "summary": None, "summarised": 0, "method": "none"}

        keep_tokens = max(int(budget * keep_fraction), 512)
        head, tail = split_head_tail(messages, keep_tokens)

        if not head:
            return {"messages": tail, "summary": None, "summarised": 0, "method": "none"}

        summary = ""
        try:
            summary = await self.summarise_head(head, chunk_budget=max(budget // 4, 1000))
        except Exception as exc:  # fall back rather than fail the turn
            log.warning("summarisation failed (%s); falling back to trimming", exc)

        if summary and total(tail, summary) <= limit:
            return {
                "messages": tail,
                "summary": summary,
                "summarised": len(head),
                "method": "summarised",
            }

        # Summary absent or still too large: drop the head outright. Worse, but it
        # keeps the turn alive, which is the whole point.
        kept = tail
        dropped = len(head)
        while len(kept) > 1 and total(kept) > limit:
            kept.pop(0)
            dropped += 1
            while len(kept) > 1 and not _clean_turn_start(kept[0]):
                kept.pop(0)
                dropped += 1
        return {"messages": kept, "summary": None, "summarised": 0,
                "method": "trimmed", "dropped": dropped}

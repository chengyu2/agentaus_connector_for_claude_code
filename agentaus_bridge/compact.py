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

import asyncio
import hashlib
import json
import re
import unicodedata
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
- Never invent detail that is not present. Omission is fine; fabrication is not.\n- Reproduce every identifier EXACTLY: file paths, names, flags, versions, region\n  codes. Use plain ASCII hyphens and quotes, never typographic ones, and never\n  wrap an identifier in $$. Put identifiers in backticks.
- Keep specifics over generalities: "set BRIDGE_PORT=9100 in .env" beats "changed config".
- Use terse bullet points grouped under short headings.
- Do not add commentary, preamble, or a closing summary.

Conversation to compact:
"""


# A single summarising pass reliably drops specifics. This second pass shows the model
# the source again alongside the summary and asks only "what is missing" - a much
# easier question to answer well than "summarise this", and it recovers detail the
# first pass elided.
GAP_INSTRUCTION = """\
Below is a SUMMARY of part of a software engineering conversation, followed by the \
ORIGINAL text it was made from.

List any concrete facts present in the ORIGINAL but missing from the SUMMARY. Focus on:
file paths, function and variable names, commands, flags, version numbers, ports, URLs, \
numeric limits, decisions and the reasons for them, bugs and their root causes, and \
anything stated as a requirement or constraint.

Output only the missing facts as terse bullet points. If nothing of substance is \
missing, output exactly: NONE

"""

# Merging concatenated per-chunk summaries produces repetition and loses ordering.
# Asking the model to reorganise them keeps the record readable as one account.
MERGE_INSTRUCTION = """\
The following are summaries of consecutive parts of one software engineering \
conversation. Merge them into a single coherent record.

- Keep every specific fact: paths, identifiers, commands, numbers, decisions and reasons.
- Remove duplication, but never drop a detail that appears only once.
- Preserve chronological order where it matters, and group related points under headings.
- Do not add anything that is not present below.

"""


Summariser = Callable[[str], Awaitable[str]]



# Models reformat punctuation when they write prose: ASCII hyphens become U+2011
# NON-BREAKING HYPHEN, quotes become curly, and identifiers sometimes arrive wrapped
# in $$...$$ as though they were mathematics. Observed from Agentaus:
#
#     EU-WEST-2        -> EU\u2011WEST\u20112
#     retry_budget_ms  -> $$retry_budget_ms$$
#
# In prose that is cosmetic. In a summary that a coding agent will read back and act
# on, it is corruption: a region code or file path carrying a typographic hyphen looks
# correct and is not, which is worse than it being missing. Normalising deterministically
# is more reliable than asking the model not to do it.
_TYPOGRAPHIC = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2212": "-", "\u00a0": " ", "\u202f": " ",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
}

_MATH_WRAPPED = re.compile(r"\$\$\s*([^$\n]{1,120}?)\s*\$\$")


def normalise_identifiers(text: str) -> str:
    """Undo typographic substitutions so identifiers survive verbatim."""
    if not text:
        return text
    text = unicodedata.normalize("NFKC", text)
    for fancy, plain in _TYPOGRAPHIC.items():
        text = text.replace(fancy, plain)
    return _MATH_WRAPPED.sub(r"`\1`", text)


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


def _chunk(text: str, budget_tokens: int, *, overlap_ratio: float = 0.08) -> list[str]:
    """Split text into pieces that each fit one summarisation call.

    Sizing is by characters throughout, never by line count. `render_for_summary`
    emits one line per message, and a single message can be tens of thousands of
    characters, so a line-counted overlap carries far more than intended: an earlier
    version overlapped 30 lines and produced chunks six times larger than the model's
    entire context window, which the API rejected outright.

    A line longer than the budget on its own is hard-split rather than emitted whole,
    for the same reason.
    """
    budget_chars = max(budget_tokens * 4, 2000)
    overlap_chars = int(budget_chars * overlap_ratio)

    units: list[str] = []
    for line in text.splitlines():
        if len(line) <= budget_chars:
            units.append(line)
        else:
            for i in range(0, len(line), budget_chars):
                units.append(line[i:i + budget_chars])

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        cost = len(unit) + 1
        if current and size + cost > budget_chars:
            chunks.append("\n".join(current))
            # Carry back a bounded tail so a point made either side of the boundary
            # reaches one summariser whole.
            carry: list[str] = []
            carried = 0
            for previous in reversed(current):
                if carried + len(previous) + 1 > overlap_chars:
                    break
                carry.insert(0, previous)
                carried += len(previous) + 1
            current, size = carry, carried
        current.append(unit)
        size += cost
    if current:
        chunks.append("\n".join(current))
    return chunks


class ConversationCompactor:
    """Replaces the head of a conversation with a summary produced by the model.

    Quality is chosen over economy throughout: chunks are summarised concurrently, a
    second pass re-reads the source to recover specifics the first pass dropped, and
    partial summaries are merged by the model rather than concatenated. The expensive
    result is cached by content hash, so the cost lands once per boundary move rather
    than once per turn.
    """

    def __init__(
        self,
        summarise: Summariser,
        *,
        cache_size: int = 64,
        max_concurrency: int = 8,
        verify: bool = True,
    ) -> None:
        self._summarise = summarise
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_size = cache_size
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._verify = verify
        self.hits = 0
        self.misses = 0
        self.calls = 0

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

    async def _call(self, prompt: str) -> str:
        """One summariser call, bounded by the concurrency limit."""
        async with self._semaphore:
            self.calls += 1
            return (await self._summarise(prompt)) or ""

    @staticmethod
    def _raw_fallback(chunk: str, limit: int = 6000) -> str:
        """Last resort when a chunk cannot be summarised at all.

        Passing the raw head through, truncated, keeps its facts in play. Returning
        nothing would silently delete that slice of the conversation, which is the
        failure this whole module exists to prevent.
        """
        if len(chunk) <= limit:
            body = chunk
        else:
            # Keep both ends: openings carry decisions, closings carry outcomes.
            half = limit // 2
            body = chunk[:half] + "\n[...]\n" + chunk[-half:]
        return "[Unsummarised excerpt - the summariser failed on this section]\n" + body

    async def _summarise_chunk(self, chunk: str, index: int, total: int) -> str:
        label = f" (part {index} of {total})" if total > 1 else ""
        summary = normalise_identifiers(
            (await self._call(SUMMARY_INSTRUCTION + label + "\n" + chunk)).strip()
        )
        if not self._verify:
            return summary

        # Second pass: ask what the first pass missed, and fold it back in. This is
        # the single biggest fidelity win - "what is missing" is a far easier question
        # for the model than "summarise well".
        gaps = normalise_identifiers((await self._call(
            GAP_INSTRUCTION + "SUMMARY:\n" + summary + "\n\nORIGINAL:\n" + chunk
        )).strip())
        if gaps and gaps.upper().strip().rstrip(".") != "NONE":
            summary = summary + "\nAdditional details:\n" + gaps
        return summary

    async def summarise_head(self, head: list, *, chunk_budget: int) -> str:
        """Summarise `head`, reusing a cached result when the same prefix recurs."""
        key = self._key(head)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self.hits += 1
            return cached

        self.misses += 1
        text = render_for_summary(head)
        pieces = _chunk(text, chunk_budget)

        # Concurrent: a long history is many independent calls, and doing them in
        # sequence is the difference between seconds and minutes of visible latency.
        #
        # return_exceptions is essential. Without it a single failed chunk - one
        # Cloudflare 524 out of nineteen calls - propagates and discards every other
        # summary that succeeded, and the whole conversation is lost to the fallback.
        raw = await asyncio.gather(*[
            self._summarise_chunk(piece, i, len(pieces))
            for i, piece in enumerate(pieces, 1)
        ], return_exceptions=True)

        summaries: list[str] = []
        failures = 0
        for piece, result in zip(pieces, raw):
            if isinstance(result, BaseException) or not str(result).strip():
                failures += 1
                log.warning("chunk summarisation failed (%s); keeping a raw excerpt",
                            type(result).__name__ if isinstance(result, BaseException)
                            else "empty reply")
                summaries.append(self._raw_fallback(piece))
            else:
                summaries.append(str(result))

        if failures:
            log.warning("%d of %d chunks fell back to raw excerpts", failures, len(pieces))
        if failures == len(pieces):
            raise RuntimeError("every chunk failed to summarise")

        if len(summaries) == 1:
            summary = summaries[0]
        else:
            joined = "\n\n---\n\n".join(summaries)
            # Merged by the model rather than concatenated: consecutive chunks overlap,
            # so raw concatenation repeats itself and reads as several disjoint records.
            summary = normalise_identifiers(
                (await self._call(MERGE_INSTRUCTION + joined)).strip()
            ) or joined

        # Condense only if the summary itself will not fit a call. Condensing merely
        # because it exceeds one chunk's budget would throw away detail that had room
        # to survive - each pass loses something.
        rounds = 0
        ceiling = max(chunk_budget * 3, 12000)
        while estimate_tokens(summary) > ceiling and rounds < 3:
            summary = normalise_identifiers((await self._call(MERGE_INSTRUCTION + summary)).strip())
            rounds += 1

        self._remember(key, summary)
        return summary

    async def compact(
        self,
        body: dict,
        *,
        limit: int,
        reserve: int,
        keep_fraction: float = 0.5,
        threshold: float = 1.0,
        chunk_tokens: int | None = None,
    ) -> dict:
        """Return a plan describing how to fit `body` into `limit`.

        `threshold` compacts before the window is actually full - at 0.8, compaction
        happens at 80% occupancy. Waiting for the hard limit means every turn near the
        boundary runs with almost no headroom for the reply, and one large tool result
        tips it over into a failure the user sees.

        Keys: `messages`, `summary` (or None), `summarised`, `method`
        ("none" | "summarised" | "trimmed").
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

        trigger = int(limit * threshold)
        if total(messages) <= trigger:
            return {"messages": messages, "summary": None, "summarised": 0, "method": "none"}

        keep_tokens = max(int(budget * keep_fraction), 512)
        head, tail = split_head_tail(messages, keep_tokens)

        if not head:
            return {"messages": tail, "summary": None, "summarised": 0, "method": "none"}

        summary = ""
        try:
            summary = await self.summarise_head(
                head, chunk_budget=chunk_tokens or max(budget // 4, 1000)
            )
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

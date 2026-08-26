"""Compressing oversized tool results before they reach Agentaus.

The thing that exhausts a 131k window is not the conversation, it is tool output. One
`Read` of a 3,000-line file, or one test run, can be 40k tokens - most of the real
headroom - and it stays in the transcript for the rest of the session, forcing a
compaction that costs a minute of visible latency every time the boundary moves.

Claude Code executes those tools and sends the result back in the *next* request, so the
bridge sees them without needing a tool of its own. Anything over the threshold is
replaced by a distilled version before translation.

Two properties make this safe rather than merely cheaper:

* **Deterministic and cached.** Tool results are immutable and re-sent every turn, so a
  distillation is keyed by `tool_use_id` + content hash and computed once. If it were
  recomputed - or worse, computed differently each turn - the conversation prefix would
  change on every request and the compactor's cache would never hit again, turning a
  saving into the 173-seconds-per-turn problem that cache was built to fix.

* **Focused only on the call itself.** The distiller is told which tool ran and with
  what input, and nothing about the current question. Conditioning on the latest user
  message would make the output change every turn, which breaks the same cache. So it
  extracts rather than answers: keep the identifiers, signatures, values and errors;
  compress the repetition.

Errors are never distilled. An `is_error` result is short and its exact text is usually
the thing being debugged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from typing import Awaitable, Callable

from .compact import _chunk, normalise_identifiers
from .gate import hold
from .translate import estimate_tokens

log = logging.getLogger("agentaus-bridge")

Summariser = Callable[[str], Awaitable[str]]


DISTIL_INSTRUCTION = """\
<tool_call>
name: {tool}
input: {input}
</tool_call>

<output>
{body}
</output>

<task>
Condense the output above so an agent can still work from it. This is an extraction task, not a \
summary: the agent will act on what you keep, so a fact you drop is a fact it no \
longer has.

Keep, exactly as written:
- Every identifier: file paths, function/class/variable names, flags, commands, versions
- Signatures and declarations, and the structure they sit in
- Error messages, stack frames, failing test names, and line numbers
- Concrete values: ports, URLs, limits, counts, timings, return values
- Anything that looks like a configuration setting or a constant

Compress:
- Repetition, boilerplate, licence headers, import blocks of no consequence
- Long runs of similar lines - say "12 more of the same shape" instead of listing them
- Prose and comments that restate what the code already says

Rules:
- Never invent anything. Omission is fine; fabrication is not.
- Reproduce identifiers EXACTLY. Plain ASCII hyphens and quotes, never typographic \
ones, and never wrap an identifier in $$. Put identifiers in backticks.
- Keep the original ordering, and keep line numbers where the input had them.
- End with one line: "[elided: <what you left out>]".
- No preamble and no commentary.
</task>

<output_format>
The condensed output only. No tags.
</output_format>
"""


MERGE_INSTRUCTION = """\
<pieces>
{body}
</pieces>

<task>
These are condensed pieces of one tool output, in order. Join them into a single record.

Keep every identifier, value and error. Remove duplication introduced by the overlap \
between pieces, but never drop something that appears only once. Preserve the order. \
Add nothing.
</task>

<output_format>
The joined record only. No tags.
</output_format>
"""


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "\n".join(parts)
    return json.dumps(content, default=str) if content is not None else ""


class ResultDistiller:
    """Replaces oversized tool results with condensed ones, cached by content."""

    def __init__(
        self,
        summarise: Summariser,
        *,
        threshold_tokens: int = 4000,
        chunk_tokens: int = 4000,
        cache_size: int = 256,
    ) -> None:
        self._summarise = summarise
        self._threshold = threshold_tokens
        self._chunk_tokens = chunk_tokens
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_size = cache_size
        self.hits = 0
        self.misses = 0
        self.calls = 0

    @staticmethod
    def _key(tool_use_id: str, text: str) -> str:
        return hashlib.sha256(f"{tool_use_id}\x00{text}".encode()).hexdigest()

    def _remember(self, key: str, value: str) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def _call(self, prompt: str) -> str:
        async with hold("distillation", "background"):
            self.calls += 1
            return (await self._summarise(prompt)) or ""

    async def _condense(self, tool: str, tool_input: str, text: str) -> str:
        pieces = _chunk(text, self._chunk_tokens)
        # Concurrent: the pieces are independent, and doing them in sequence is the
        # difference between seconds and a minute of latency on one large tool result.
        # Measured: four sequential chunks added ~60s to a single turn.
        raw = await asyncio.gather(*[
            self._call(DISTIL_INSTRUCTION.format(
                tool=tool, input=tool_input[:400], body=piece))
            for piece in pieces
        ])
        parts = [p for p in (normalise_identifiers((r or "").strip()) for r in raw) if p]
        if not parts:
            raise RuntimeError("distiller produced nothing")
        if len(parts) == 1:
            return parts[0]
        merged = await self._call(MERGE_INSTRUCTION.format(body="\n\n---\n\n".join(parts)))
        return normalise_identifiers(merged.strip()) or "\n\n".join(parts)

    def _tool_for(self, messages: list) -> dict:
        """Map tool_use_id -> (name, input) so a result can say what produced it."""
        index: dict = {}
        for message in messages or []:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    index[block.get("id") or ""] = (
                        block.get("name") or "?",
                        json.dumps(block.get("input") or {}, default=str),
                    )
        return index

    async def distill(self, body: dict) -> dict:
        """Return `body` with oversized tool results condensed. Never raises."""
        messages = body.get("messages") or []
        index = self._tool_for(messages)

        saved = 0
        out_messages = []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                out_messages.append(message)
                continue

            blocks = []
            changed = False
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    blocks.append(block)
                    continue
                # An error is short, and its exact wording is what is being debugged.
                if block.get("is_error"):
                    blocks.append(block)
                    continue

                text = _text_of(block.get("content"))
                before = estimate_tokens(text)
                if before <= self._threshold:
                    blocks.append(block)
                    continue

                call_id = block.get("tool_use_id") or ""
                key = self._key(call_id, text)
                condensed = self._cache.get(key)
                if condensed is not None:
                    self._cache.move_to_end(key)
                    self.hits += 1
                else:
                    self.misses += 1
                    tool, tool_input = index.get(call_id, ("unknown tool", "{}"))
                    try:
                        condensed = await self._condense(tool, tool_input, text)
                    except Exception as exc:
                        # The raw result is worse for the window but correct. Failing
                        # the turn over a compression step would be a bad trade.
                        log.warning("distillation failed for %s (%s); keeping the raw "
                                    "result", call_id, exc)
                        blocks.append(block)
                        continue
                    self._remember(key, condensed)

                after = estimate_tokens(condensed)
                # A distillation that grew is not one worth having.
                if after >= before:
                    blocks.append(block)
                    continue

                saved += before - after
                changed = True
                blocks.append({
                    **block,
                    "content": (
                        f"[Condensed by the bridge from ~{before:,} tokens of output. "
                        f"Identifiers, values and errors were kept verbatim; repetition "
                        f"was compressed.]\n{condensed}"
                    ),
                })

            out_messages.append({**message, "content": blocks} if changed else message)

        if saved:
            log.info(
                "distilled tool results: saved ~%d tokens (%d cached, %d computed)",
                saved, self.hits, self.misses,
            )
            return {**body, "messages": out_messages}
        return body

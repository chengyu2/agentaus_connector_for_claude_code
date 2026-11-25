"""Fitting a conversation into Agentaus' context window: compaction, distillation and clipping."""

from __future__ import annotations

import json
import logging

from fastapi import Request

from . import helper_calls

from .compact import ConversationCompactor
from .config import settings
from .conversation_text import _head_and_tail
from .distill import ResultDistiller
from .request_log import rlog
from .tokens import calibrator
from .translate import estimate_request_tokens, estimate_tokens, raw_token_count


def _get_compactor(request: Request) -> ConversationCompactor:
    """One compactor per process, so its summary cache survives between turns.

    Without the cache the same conversation prefix would be re-summarised on every
    turn, since Claude Code re-sends the whole history each time.
    """
    existing = getattr(request.app.state, "compactor", None)
    if existing is None:
        client = request.app.state.client
        existing = ConversationCompactor(
            lambda text: helper_calls._agentaus_summarise(client, text),
            verify=settings.agentaus_verify_summary,
            block=settings.agentaus_compact_block,
        )
        request.app.state.compactor = existing
    return existing


def _get_distiller(request: Request) -> ResultDistiller:
    """One distiller per process, so its cache survives between turns.

    Without the cache each turn would re-condense the same immutable tool results, which
    is both wasted spend and - worse - a conversation prefix that changes every request,
    which would stop the compactor's cache ever hitting again.
    """
    existing = getattr(request.app.state, "distiller", None)
    if existing is None:
        client = request.app.state.client
        existing = ResultDistiller(
            lambda text: helper_calls._agentaus_summarise(client, text),
            threshold_tokens=settings.agentaus_distill_threshold_tokens,
            chunk_tokens=settings.agentaus_distill_chunk_tokens,
        )
        request.app.state.distiller = existing
    return existing


def _calibrate_from_usage(body: dict, usage: dict | None) -> None:
    """Compare our count of a request against the count Agentaus charged for it.

    This is the only ground truth available: Agentaus does not publish its tokeniser,
    so the ratio between our count and its reported `input_tokens` is what makes the
    context arithmetic converge on reality.
    """
    if not isinstance(usage, dict):
        return
    reported = usage.get("input_tokens")
    if not isinstance(reported, int) or reported <= 0:
        return
    counted = raw_token_count(
        json.dumps(
            {"s": body.get("system"), "m": body.get("messages"), "t": body.get("tools")},
            default=str,
        )
    )
    calibrator.observe(counted, reported)


def _with_summary(system, summary: str, replaced: int):
    """Fold the summary into the system prompt, replacing the messages it stands for."""
    notice = (
        f"\n\n[Earlier conversation summary - the {replaced} oldest messages were "
        f"replaced by this compacted record to fit the context window. Treat it as an "
        f"accurate account of what happened before; if the user refers to something "
        f"not covered here, say so rather than guessing.]\n{summary}"
    )
    if system is None:
        return notice.strip()
    if isinstance(system, str):
        return system + notice
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": notice.strip()}]
    return system


def _with_trim_notice(system, dropped: int):
    """Append a note to the system prompt saying history was trimmed.

    Without this the model answers as though it can see the whole conversation and
    may invent continuity it has no basis for.
    """
    notice = (
        f"\n\n[Note: the {dropped} oldest message(s) of this conversation were removed "
        f"to fit the context window. If the user refers to something earlier that you "
        f"cannot see, say so rather than guessing.]"
    )
    if system is None:
        return notice.strip()
    if isinstance(system, str):
        return system + notice
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": notice.strip()}]
    return system


async def _fit_to_window(
    request: Request, body: dict, *, limit: int, reserve: int, scale: float
) -> dict:
    """Compact `body` until it should fit `limit * scale`, and return it.

    `scale` tightens the target on a retry: if Agentaus rejected the request as too
    long, our estimate was optimistic, so the next attempt aims lower rather than
    trusting the same arithmetic twice.

    Never raises and never refuses - the worst case returns the body compacted as far
    as it can be, and lets the API decide.
    """
    if limit <= 0 or not settings.agentaus_auto_trim:
        return body

    target = max(int(limit * scale), 1000)
    threshold = settings.agentaus_compact_threshold
    estimated = estimate_request_tokens(body)
    if estimated + reserve <= int(target * threshold):
        return body

    plan = await _get_compactor(request).compact(
        body,
        limit=target,
        reserve=reserve,
        keep_fraction=settings.agentaus_keep_fraction,
        threshold=threshold,
        chunk_tokens=settings.agentaus_summary_chunk_tokens,
    )
    if plan["method"] == "summarised":
        fitted = {**body, "messages": plan["messages"]}
        fitted["system"] = _with_summary(
            body.get("system"), plan["summary"], plan["summarised"]
        )
        rlog(logging.WARNING, 
            "compacted %d oldest message(s) into a summary for a %d-token target "
            "(~%d -> ~%d tokens)",
            plan["summarised"], target, estimated, estimate_request_tokens(fitted),
        )
        return _clipped_to(fitted, target, reserve)
    if plan["method"] == "trimmed":
        fitted = {**body, "messages": plan["messages"]}
        fitted["system"] = _with_trim_notice(body.get("system"), plan["dropped"])
        rlog(logging.WARNING, 
            "summarisation could not reach the %d-token target; dropped %d oldest "
            "message(s) instead (~%d -> ~%d tokens)",
            target, plan["dropped"], estimated, estimate_request_tokens(fitted),
        )
        return _clipped_to(fitted, target, reserve)
    return _clipped_to(body, target, reserve)


def _tool_result_text(block: dict) -> str | None:
    text = block.get("content")
    if isinstance(text, list):
        if not all(isinstance(b, dict) and b.get("type") == "text" for b in text):
            return None
        return "\n".join(b.get("text", "") for b in text)
    return text if isinstance(text, str) else None


_CLIP_FLOOR_CHARS = 4000


def _clip_largest_tool_results(body: dict, target: int) -> tuple[dict, int]:
    """`body` with its largest tool results cut to head and tail until it fits `target` tokens.

    Measured (d12 c1913): one 400 KB `ls -R` result sat in the unfinished last exchange, which
    compaction never touches ("compaction result: 485857 -> 485857"), and every request after
    it was rejected as too long. Returns (body, how many results were clipped).
    """
    clipped = 0
    for _ in range(8):
        over = estimate_request_tokens(body) - target
        if over <= 0:
            break
        found = None
        for i, message in enumerate(body.get("messages") or []):
            if message.get("role") != "user" or not isinstance(message.get("content"), list):
                continue
            for j, block in enumerate(message["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = _tool_result_text(block)
                    if text and len(text) > _CLIP_FLOOR_CHARS and (
                            found is None or len(text) > len(found[2])):
                        found = (i, j, text)
        if found is None:
            break
        i, j, text = found
        tokens = max(estimate_tokens(text), 1)
        # Cut what is over, and a tenth more for the estimate, never below the floor.
        keep = max(int(len(text) * max(tokens - over, 0) / tokens * 0.9), _CLIP_FLOOR_CHARS)
        if keep >= len(text):
            break
        short = _head_and_tail(text, keep * 2 // 3, keep // 3).replace(
            " characters ...]", " characters cut to fit the context window. Print less: "
            "counts, head, or the lines you need ...]", 1)
        messages = list(body["messages"])
        blocks = list(messages[i]["content"])
        blocks[j] = {**blocks[j], "content": short}
        messages[i] = {**messages[i], "content": blocks}
        body = {**body, "messages": messages}
        clipped += 1
    return body, clipped


def _clipped_to(body: dict, target: int, reserve: int) -> dict:
    """`body`, its largest tool results clipped if it still does not fit after fitting."""
    room = max(target - reserve, target // 2)
    if estimate_request_tokens(body) <= room:
        return body
    clipped_body, clipped = _clip_largest_tool_results(body, room)
    if clipped:
        rlog(logging.WARNING, "still over the window after fitting; clipped %d large tool "
             "result(s) to head and tail (~%d -> ~%d tokens)", clipped,
             estimate_request_tokens(body), estimate_request_tokens(clipped_body))
    return clipped_body

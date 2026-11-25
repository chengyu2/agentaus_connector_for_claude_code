"""Side calls to the model that plan a turn or check and rewrite an answer:
syntax fixes, grounding, self-review."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from . import gate
from . import ledger
from . import prompt_style
from . import syntax
from . import tools as bridge_tools
from . import upstream_http

from .augment import (
    ADJUDICATE_INSTRUCTION,
    GROUNDING_INSTRUCTION,
    GROUNDING_ONE_PASS_INSTRUCTION,
    REVIEW_INSTRUCTION,
    REVISE_INSTRUCTION,
    STRIP_UNGROUNDED_INSTRUCTION,
    awaiting_background,
    declared_verdict,
    grounding_revision,
    grounding_verdict,
    is_subagent_turn,
    plan_prompt,
    worth_grounding_check,
    worth_reviewing,
    worth_reviewing_turn,
)
from .config import settings
from .conversation_text import _last_user_text
from .request_log import rlog
from .upstream_http import _agentaus_headers, _completion_chunks, _learn_limit_from


HELPER_SYSTEM = (
    "You are a precise assistant working inside a software engineering tool. Do exactly "
    "what the message asks, in exactly the output format it asks for. No preamble, no "
    "commentary, no questions back."
)


async def _agentaus_summarise(
    client: httpx.AsyncClient, text: str, *, on_piece=None, stream: bool | None = None
) -> str:
    """Ask Agentaus to compact a slice of the conversation.

    Deliberately delegated to the model rather than to heuristics here: what is worth
    keeping out of an engineering conversation is a judgement call, and the model is
    better placed to make it than any rule the bridge could hard-code.
    """
    payload = {
        # A system message is what `system_prompt_overwrite` overwrites WITH. Sent without
        # one, Agentaus keeps its own ~2,200-token general-assistant persona: measured,
        # "Say hi" cost 2,442 input tokens bare and 207 with a one-line system message,
        # and with tools offered the bare form refused to use them. Every helper call -
        # plan, review, search chunk, compaction - paid that and carried the persona.
        "messages": [{"role": "system", "content": HELPER_SYSTEM},
                     {"role": "user", "content": text}],
        "stream": settings.agentaus_stream_helpers if stream is None else stream,
        "system_prompt_overwrite": True,
    }
    started_at = time.monotonic()

    limit = settings.agentaus_helper_timeout_seconds
    if limit > 0:
        try:
            return await asyncio.wait_for(
                _helper_call(client, payload, text, started_at, on_piece), timeout=limit
            )
        except asyncio.TimeoutError:
            rlog(logging.WARNING, "helper call exceeded %.0fs and was abandoned", limit)
            raise RuntimeError(f"helper call timed out after {limit:.0f}s")
    return await _helper_call(client, payload, text, started_at, on_piece)


async def _helper_call(
    client: httpx.AsyncClient, payload: dict, text: str, started_at: float, on_piece=None
) -> str:
    """One helper call, streamed or buffered. Bounded by the caller."""
    if payload["stream"]:
        # Streamed rather than buffered. A buffered helper call holds a connection open
        # while the server composes the entire reply, and a fan-out of those is what
        # saturates Agentaus - a batch run once queued ~135 of them and stopped making
        # progress at all. Streaming returns the first bytes as they are generated, so a
        # slow reply stops looking like a dead one and the connection drains sooner.
        try:
            return await _stream_helper(client, payload, text, started_at, on_piece)
        except (httpx.HTTPError, RuntimeError) as exc:
            # Falling back rather than failing: a streaming fault must not cost the
            # caller its summary when the buffered path still works.
            rlog(logging.WARNING, "streamed helper call failed (%s); retrying buffered", exc)
            payload = {**payload, "stream": False}

    response = await upstream_http._post_with_retry(
        client, settings.agentaus_url, json_body=payload, headers=_agentaus_headers(),
        retry_capacity=False,
    )
    if response.status_code >= 400:
        rlog(logging.WARNING, "helper call failed HTTP %d after %.1fs",
             response.status_code, time.monotonic() - started_at)
        raise RuntimeError(f"summariser returned HTTP {response.status_code}")
    rlog(logging.DEBUG, "helper call ok in %.1fs (%d chars in)",
         time.monotonic() - started_at, len(text))
    data = response.json()
    if isinstance(data.get("error"), dict):
        _learn_limit_from(str(data["error"].get("message") or ""))
        raise RuntimeError(str(data["error"].get("message"))[:200])
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


class _HelperText(str):
    """A helper reply that remembers how it ended. Still a plain `str` to every caller; a
    pass that would REPLACE an answer with it reads `finish_reason`, because a reply cut
    off at the token limit reads as a finished one."""

    finish_reason: str | None = None


async def _stream_helper(
    client: httpx.AsyncClient, payload: dict, text: str, started_at: float, on_piece=None
) -> str:
    """Consume a streamed helper reply and return the assembled content."""
    parts: list[str] = []
    first_byte = 0.0
    finish: str | None = None
    async with client.stream(
        "POST", settings.agentaus_url, json=payload, headers=_agentaus_headers()
    ) as upstream:
        if upstream.status_code >= 400:
            detail = (await upstream.aread()).decode("utf-8", "replace")[:300]
            _learn_limit_from(detail)
            raise RuntimeError(f"HTTP {upstream.status_code}: {detail}")
        async for chunk in _completion_chunks(upstream):
            # Agentaus reports some failures as HTTP 200 with an error object inside the
            # stream; without this the reply looks empty and successful.
            if isinstance(chunk.get("error"), dict):
                message = str(chunk["error"].get("message") or "")
                _learn_limit_from(message)
                raise RuntimeError(message[:200] or "error inside the helper stream")
            for choice in chunk.get("choices") or []:
                finish = choice.get("finish_reason") or finish
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    if not parts:
                        first_byte = time.monotonic() - started_at
                    parts.append(piece)
                    if on_piece is not None:
                        on_piece(piece)
    rlog(logging.DEBUG, "helper stream ok in %.1fs (first byte %.1fs, %d chars in)",
         time.monotonic() - started_at, first_byte, len(text))
    reply = _HelperText("".join(parts))
    reply.finish_reason = finish
    return reply


def _without_bridge_tools(payload: dict) -> dict:
    """The payload with the bridge's own tools taken off, and a turn asking it to settle.

    The first answer to a spent search budget. Taking every tool away ended the agent's
    turn even when its next step was to run a test or make the edit, so only the tools
    the budget counts are withdrawn: Claude Code's stay, and a call to one of them goes
    to the client like any other. Only when the model still asks for a search is it
    asked again with nothing at all - see _answer_without_tools.
    """
    kept = [t for t in payload.get("tools") or []
            if ((t or {}).get("function") or {}).get("name") not in bridge_tools.BRIDGE_TOOLS]
    out = {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")}
    if kept:
        out["tools"] = kept
        out["tool_choice"] = "auto"
    out["messages"] = list(out.get("messages") or []) + [{
        "role": "user",
        "content": prompt_style.section(
            "task",
            "Your search budget for this turn is spent, and the search tools are gone. "
            "Do not search again, with Bash or any other tool: what you found is above."
            "\n\nIf it is enough, give the answer now, in the format originally asked "
            "for. If the task needs a step that is not a search - running a test, "
            "reading a file you already know, making an edit - take it with the tools "
            "you still have.",
        ),
    }]
    return out


async def _answer_without_tools(client: httpx.AsyncClient, payload: dict) -> str:
    """Ask for an answer from what is already in the conversation, tools removed.

    The last resort, where the model has the material and will not settle: it still asked
    for a search after _without_bridge_tools took the search tools away, or it returned
    nothing twice. Taking the tools off the request removes the option it keeps choosing,
    and everything it found is still in the messages.

    Returns "" on any failure, so the caller can fall back to saying what happened.
    """
    stripped = {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")}
    messages = list(stripped.get("messages") or [])
    messages.append({
        "role": "user",
        "content": prompt_style.section(
            "task",
            "Answer now, from what is already above. You have no tools for this "
            "turn.\n\nIf what you gathered is enough, give the answer in the format "
            "originally asked for. If it genuinely is not, say in one line what is "
            "missing - do not describe your search.",
        ),
    })
    stripped["messages"] = messages
    stripped["stream"] = False
    try:
        async with gate.hold("forced answer", "urgent"):
            response = await upstream_http._post_with_retry(
                client, settings.agentaus_url,
                json_body=stripped, headers=_agentaus_headers(),
                retry_capacity=False,
            )
        if response.status_code >= 400:
            return ""
        data = response.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if text.strip():
            rlog(logging.INFO, "forced answer produced %d chars", len(text))
        return text.strip()
    except Exception as exc:
        rlog(logging.WARNING, "forced answer failed (%s)", exc)
        return ""


# What a rewrite pass is shown of an answer. A pass shown only part of one must not
# replace all of it: the final synthesis of a workflow's results runs past this, and a
# revision written from its first 12,000 characters silently dropped the rest.
_REWRITE_CHARS = 12000


def _too_long_to_rewrite(answer: str, what: str) -> bool:
    if len(answer or "") <= _REWRITE_CHARS:
        return False
    rlog(logging.INFO, "%s skipped: the answer is %d chars, past the %d a rewrite sees",
         what, len(answer), _REWRITE_CHARS)
    return True


async def _fix_syntax(client: httpx.AsyncClient, answer: str, *, strict: bool = False) -> str:
    """If the answer's Python does not parse, ask once for the typo to be fixed.

    Deterministic detection, so this costs nothing on an answer that is fine. Any failure
    returns the original: a broken repair must not lose a mostly-correct answer.
    """
    error = syntax.first_error(answer, strict=strict)
    if not error or _too_long_to_rewrite(answer, "syntax fix"):
        return answer
    rlog(logging.WARNING, "answer does not parse (%s); asking for a fix", error)
    try:
        async with gate.hold("syntax fix", "urgent"):
            fixed = await _agentaus_summarise(
                client, syntax.FIX_INSTRUCTION.format(error=error, answer=answer[:12000])
            )
    except Exception as exc:
        rlog(logging.WARNING, "syntax fix failed (%s); keeping the answer", exc)
        return answer
    if not fixed or not fixed.strip():
        return answer
    if syntax.first_error(fixed):
        # Still broken. The original is no worse and may be closer to right.
        rlog(logging.WARNING, "the fix does not parse either; keeping the original")
        return answer
    rlog(logging.INFO, "syntax fixed (%d -> %d chars)", len(answer), len(fixed))
    return fixed.strip()


def _grounding_applies(body: dict, answer: str, ran: dict | None) -> bool:
    """Whether to check this answer against the evidence rather than self-review it.

    A turn whose facts came from the bridge's own tools is a tool turn, even though no
    client tool appears in the message list. Treated as a prose turn, it went to the
    self-review, which sees only the request and the answer. Observed: an answer citing
    `src/retry.py` lines 7-9, just read by `agentaus_zoom`, was "revised" into a
    description of urllib3's `Retry.get_backoff_time()` - from memory, in a project that
    does not contain urllib3.
    """
    # A subagent's answer is the value its parent reads; a rewrite would replace it.
    # And a reply that only reports a background launch makes no claim to check: graded
    # as one, "the workflow is running" became "the repository may contain a workflow;
    # I have not examined it".
    if is_subagent_turn(body) or awaiting_background(body):
        return False
    minimum = settings.agentaus_review_min_chars
    if ran:
        return len((answer or "").strip()) >= minimum
    return worth_grounding_check(body, answer, min_chars=minimum)


def _bridge_evidence(ran: dict | None, *, result_chars: int = 2500,
                     budget: int = 24000) -> str:
    """What the bridge's own tools returned this turn, in the evidence ledger's shape.

    The ledger is built from the message list Claude Code sent, and bridge tool calls
    never enter it - they run inside the turn. So a fact found by `agentaus_search` or
    read with `agentaus_zoom` looked unsupported. Observed: an answer stating
    `CACHE_TTL_SECONDS = 600` in `src/config.py`, read by `agentaus_zoom` a minute
    earlier, was judged to "need a tool read of src/config.py" and rewritten to "I have
    not read it" - a correct, evidenced answer destroyed by the pass meant to protect it.
    """
    if not ran:
        return ""
    blocks: list[str] = []
    spent = 0
    for signature, result in reversed(list(ran.items())):
        try:
            key = json.loads(signature)
        except ValueError:
            key = {}
        name = key.get("n") or "bridge tool"
        args = json.dumps(key.get("a") or {}, default=str, ensure_ascii=False)[:200]
        room = min(result_chars, max(0, budget - spent))
        body = ledger.excerpt(result, room) if room and (result or "").strip() else ""
        spent += len(body)
        head = f"- {name}({args}) -> ok"
        blocks.append(f"{head}\n  <output>\n{body}\n  </output>" if body else head)
    blocks.reverse()
    return (
        f"\n[{len(ran)} bridge tool call(s) ran inside this turn on the agent's behalf - "
        "searches and file reads. Each is followed by what it returned. A value that "
        "appears in an <output> below IS supported: the agent read it there.]\n"
        + "\n".join(blocks)
    )


# A grounding revision shorter than this share of the answer is not used. The pass is told
# to strip unsupported claims and keep the rest, so a sound revision is most of the
# original; one under half of it is far likelier a reply cut off mid-answer, or a note ABOUT
# the answer ("REVISED" then "removed the claim about X") than an answer that was mostly
# invented. Keeping the original loses only a cleanup; using the stub loses the answer.
_MIN_REVISION_SHARE = 0.5


def _revision_usable(answer: str, revised: str, reply: str) -> bool:
    """Whether a grounding rewrite may replace `answer`. `reply` is the helper reply it came
    from, which says whether the model stopped on its own or hit the token limit."""
    if getattr(reply, "finish_reason", None) == "length":
        rlog(logging.WARNING, "grounding revision was cut off at the token limit; "
             "keeping the answer")
        return False
    if len(revised) < _MIN_REVISION_SHARE * len(answer):
        rlog(logging.WARNING, "grounding revision is %d chars against the answer's %d; "
             "keeping the answer", len(revised), len(answer))
        return False
    return True


async def _check_grounding(
    client: httpx.AsyncClient, body: dict, answer: str, bridge_ran: dict | None = None
) -> str:
    """Check an answer against what the turn actually ran, and strip what it cannot support.

    This is the pass ordinary self-review cannot be: the reviewer is given the ledger of
    tools actually executed, so "you claim this about a file you never opened" becomes a
    checkable statement rather than a guess.

    It exists because the ordinary review has to sit out these turns - a reviewer shown
    only the request and the answer reads a well-grounded reply as an unverified claim -
    and that left the one kind of turn where claims outrun evidence with nothing watching
    it at all. Observed: a survey of a repository asserting what was in files it had never
    read, and citing a policy that does not exist.

    Returns the answer to use. Any failure returns the original: a broken check must never
    cost a good answer.
    """
    ran = "\n".join(part for part in (
        ledger.render_evidence(body.get("messages") or [], limit=60),
        ledger.render_notifications(body.get("messages") or []),
        _bridge_evidence(bridge_ran),
    ) if part)
    if not ran or _too_long_to_rewrite(answer, "grounding check"):
        return answer
    if settings.agentaus_grounding_one_pass:
        try:
            async with gate.hold("grounding check", "background"):
                reply = await _agentaus_summarise(
                    client,
                    GROUNDING_ONE_PASS_INSTRUCTION.format(ledger=ran, answer=answer[:12000]),
                )
            revised = grounding_revision(reply)
            if revised and _revision_usable(answer, revised, reply):
                rlog(logging.WARNING, "grounding check found unsupported claims (one pass)")
                rlog(logging.INFO, "stripped ungrounded claims (%d -> %d chars)",
                     len(answer), len(revised))
                return revised
        except Exception as exc:
            rlog(logging.WARNING, "grounding check failed (%s); keeping the answer", exc)
        return answer
    try:
        async with gate.hold("grounding check", "background"):
            reply = await _agentaus_summarise(
                client,
                GROUNDING_INSTRUCTION.format(ledger=ran, answer=answer[:12000]),
            )
        gaps = grounding_verdict(reply)
        if not gaps:
            return answer
        rlog(logging.WARNING, "grounding check found unsupported claims:\n%s", gaps[:600])
        async with gate.hold("grounding rewrite", "background"):
            revised = await _agentaus_summarise(
                client,
                STRIP_UNGROUNDED_INSTRUCTION.format(
                    answer=answer[:12000], gaps=gaps[:4000]
                ),
            )
        if (revised and revised.strip()
                and _revision_usable(answer, revised.strip(), revised)):
            rlog(logging.INFO, "stripped ungrounded claims (%d -> %d chars)",
                 len(answer), len(revised))
            return revised.strip()
    except Exception as exc:
        rlog(logging.WARNING, "grounding check failed (%s); keeping the answer", exc)
    return answer


def _answer_check(client: httpx.AsyncClient, original: dict, answer: str, ran: dict):
    """The rewriting pass a final streamed answer gets, as a coroutine, or None: self-review
    for a turn that used no tools, else the grounding check when there is evidence to check
    it against."""
    if (settings.agentaus_self_review and not ran and worth_reviewing_turn(original)
            and worth_reviewing(answer, min_chars=settings.agentaus_review_min_chars)):
        return _self_review(client, _last_user_text(original), answer)
    if settings.agentaus_grounding_check and _grounding_applies(original, answer, ran):
        # The turns review has to sit out are the turns where a claim can outrun the
        # evidence. This checks against the tools actually run instead of guessing.
        return _check_grounding(client, original, answer, ran)
    return None


async def _self_review(client: httpx.AsyncClient, request_text: str, answer: str) -> str:
    """Have Agentaus critique its own answer and revise it if defects are found.

    Returns the answer to use - the original when the review is clean, or a revision.
    Any failure returns the original: a broken review must never lose a good answer.
    """
    if _too_long_to_rewrite(answer, "self-review"):
        return answer
    try:
        async with gate.hold("self-review", "background"):
            review = await _agentaus_summarise(
                client,
                REVIEW_INSTRUCTION.format(
                    request=request_text[:12000], answer=answer[:12000]
                ),
            )
        verdict = declared_verdict(review)
        if verdict is None:
            # The reviewer did not follow the format. Ask it rather than guessing from
            # the prose: sniffing for "OK" misreads both "OK, but the empty case is
            # broken" and a bare approval wrapped in markdown, and the two mistakes
            # fail in opposite directions.
            async with gate.hold("review adjudication", "background"):
                adjudication = await _agentaus_summarise(
                    client, ADJUDICATE_INSTRUCTION.format(review=review[:6000])
                )
            verdict = not adjudication.strip().upper().startswith("YES")
            rlog(logging.INFO, "review verdict was unstated; adjudicated as %s",
                     "sound" if verdict else "defective")
        if verdict:
            return answer
        async with gate.hold("revision", "background"):
            revised = await _agentaus_summarise(
                client,
                REVISE_INSTRUCTION.format(
                    request=request_text[:12000], answer=answer[:12000],
                    defects=review[:6000],
                ),
            )
        if revised and revised.strip():
            rlog(logging.INFO, "self-review revised the answer (%d -> %d chars)", len(answer), len(revised))
            return revised.strip()
    except Exception as exc:
        rlog(logging.WARNING, "self-review failed (%s); keeping the original answer", exc)
    return answer


async def _plan_turn(client: httpx.AsyncClient, body: dict, on_piece=None,
                     priority: str = "urgent") -> str:
    """Ask Agentaus to plan this turn before it answers it.

    Agentaus has no native thinking mode, so left alone it answers from the first thing
    that comes to mind - the documented "started editing before working out what the
    change required" failure. Asking for the plan as its own turn is the same trade the
    review pass makes: two cheap passes beat one on a smaller model.

    Never raises. A failed plan returns "" and the turn proceeds unplanned, exactly as
    it does today - a broken planner must not cost the user their answer.

    `priority` is "urgent" when the turn is blocked on the plan. A plan written beside the
    turn's first call (plans.py) blocks nothing, so it passes "normal": as urgent it took
    the gate ahead of the calls the turn is actually waiting on.
    """
    try:
        async with gate.hold("planning", priority):
            # Streamed when someone is watching it form, so the plan appears as it is
            # written rather than all at once after five to fifteen silent seconds.
            plan = await _agentaus_summarise(
                client, plan_prompt(_last_user_text(body), body,
                                    routing=settings.agentaus_skill_routing),
                on_piece=on_piece, stream=True if on_piece is not None else None,
            )
    except Exception as exc:
        rlog(logging.WARNING, "planning pass failed (%s); answering unplanned", exc)
        return ""
    plan = (plan or "").strip()
    if plan:
        rlog(logging.INFO, "planned the turn in %d chars", len(plan))
    return plan


def _checks_apply(body: dict) -> bool:
    """Whether the bridge's answer-rewriting passes may touch this request at all.

    Only agent turns, which always carry tools. A tool-less request is Claude Code
    talking to the model for its own purposes - classifying whether a turn is blocked,
    naming the session - and the answer's exact shape is the point. See
    AGENTAUS_REVIEW_TOOLLESS.
    """
    return bool(body.get("tools")) or settings.agentaus_review_toolless

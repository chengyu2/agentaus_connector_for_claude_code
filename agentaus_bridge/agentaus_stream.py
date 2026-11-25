"""Turning Agentaus' streamed reply into an Anthropic SSE stream,
with tool rounds and answer checks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Callable

import httpx

from . import batching
from . import finish
from . import gate
from . import graph
from . import harmony
from . import helper_calls
from . import ledger
from . import stall_check
from . import syntax
from . import throttle
from . import tool_handling
from . import upstream_http

from .augment import (
    CLASSIFY_REFUSAL_INSTRUCTION,
    MISSING_DELIVERABLES_CORRECTION,
    REFUSAL_CORRECTION,
    STALLED_CORRECTION,
    awaiting_background,
    early_deliverable_writes,
    is_subagent_turn,
    mid_tool_loop,
    missing_deliverables,
    read_turn_verdict,
    working_directory_of,
    writes_before_workflow,
)
from .bridge_responses import _error_type_for_status
from .config import settings
from .context_window import _calibrate_from_usage
from .conversation_text import _head_and_tail, _last_user_text, _original_request
from .graph_turn import _graph_main_loop, _graph_reruns, _run_graph_instead
from .helper_calls import _answer_without_tools, _checks_apply, _fix_syntax, _without_bridge_tools
from .request_log import rlog
from .stall_check import _may_reask_stalled, _stall_key, _structural_stall
from .stream_delivery import _lines_beating, _waiting
from .text import normalise_for_display
from .tool_handling import (
    _correction_for,
    _guard_imports,
    _known_tool_names,
    _on_the_wire,
    _partition_tool_calls,
    _reads_first,
    _tool_schemas,
    _validate_tool_calls,
    _with_tool_results,
    _with_unrun_calls,
)
from .translate import AnthropicStreamBuilder, ToolCallAccumulator, estimate_request_tokens
from .upstream_http import (
    _RETRYABLE_STATUS,
    _agentaus_error_text,
    _agentaus_headers,
    _describe,
    _is_over_length,
    _is_retryable_exception,
    _is_transient,
    _learn_limit_from,
    _sleep_before_retry,
    _watching_for_stall,
)


async def _unguarded_event_stream(
    client: httpx.AsyncClient,
    payload: dict,
    original: dict,
    model: str,
    started: float,
    refit=None,
    plan: str | None = None,
    builder: AnthropicStreamBuilder | None = None,
    library: frozenset = frozenset(),
    plain_text: bool = False,
    client_body: dict | None = None,
    stop_retrying: Callable[[], bool] | None = None,
    time_up_text: Callable[[], str] | None = None,
    request_kind: str = "normal",
    checks: set | None = None,
) -> AsyncIterator[bytes]:
    """Produce a valid Anthropic SSE stream from an Agentaus response.

    `builder` is passed when the response has already started - message_start sent and
    the plan streamed live - so this continues it rather than beginning a second one.
    `original` is the prepared (fitted, possibly compacted) body; `client_body` the body as
    the client sent it, which the write guards and the graph's task are read from.

    `stop_retrying()` True means a gateway failure is not replayed: the turn is answered with
    `time_up_text()` instead (a graph agent past its limit - see _handle_agentaus).
    `request_kind` is what this turn is to the throttle (throttle.REQUEST_KIND).
    """
    received = client_body if client_body is not None else original
    if builder is None:
        builder = AnthropicStreamBuilder(
            model,
            input_tokens=estimate_request_tokens(original),
            chunk_chars=settings.chunk_chars,
        )
        yield builder.start()

    # The plan leads the message, where a native thinking block would. Emitted before
    # the upstream call rather than after it, so the user sees the model's reasoning
    # while the answer is still being generated instead of all at once at the end.
    if plan:
        # Either way the reasoning is visible. A thinking block is the better rendering;
        # tagged text is the fallback for a client that will not show one.
        yield (builder.thinking(plan) if settings.agentaus_thinking_visible
               else builder.plan_as_text(plan))

    finish_reason: str | None = None
    usage: dict | None = None
    accumulator = ToolCallAccumulator()

    # A transient fault before the first content byte can be retried invisibly: the
    # client has only seen `message_start`, which carries no content. Once any text or
    # tool call has been emitted a retry would duplicate it, so `emitted` latches the
    # stream to fail-fast from that point on.
    # Buffer the answer when it may need revising. Tool turns are never buffered:
    # they carry no prose to review and the client is waiting on the tool_use.
    # Held back only when a check may rewrite it; anything else streams as it arrives.
    # Any of the passes that may rewrite it: tied to self-review alone, turning review off
    # silently turned off the grounding check and the syntax fix as well - the buffered path
    # never did that.
    buffering = (settings.agentaus_self_review or settings.agentaus_grounding_check
                 or settings.agentaus_syntax_check) and _checks_apply(original)
    pending: list[str] = []

    # While the answer is held back, its tokens still go to the client as they arrive,
    # in a thinking block - see AnthropicStreamBuilder.live_thinking.
    live = buffering and settings.agentaus_live_draft
    live_started = False

    def draft(text: str) -> bytes:
        nonlocal live_started
        if not live or not text:
            return b""
        if not live_started:
            live_started = True
            lead = "\n\n" if builder.open_kind == "thinking" else ""
            text = lead + "_Live draft from Agentaus. The checked answer follows._\n\n" + text
        return builder.live_thinking(text)

    def progress(note: str) -> bytes:
        # A line in the draft saying what the bridge is doing between upstream calls, so
        # a long search reads as work in progress rather than a stalled turn.
        return draft(f"\n\n_{note}_\n\n") if live else b""

    # Tracks recompaction attempts driven by Agentaus rejecting the prompt as too long.
    fit_attempt = 0
    fit_scale = 1.0

    # Rounds of bridge-executed tool calls. `agentaus_search` is answered here and
    # re-asked upstream; the client never learns the call happened. Bounded, so a
    # model that keeps searching still has to produce an answer eventually.
    tool_round = 0
    corrections = 0
    ran: dict = {}
    # Each re-ask below happens once: the bridge's tools withdrawn at the round limit,
    # and an empty reply sent again unchanged.
    cut_off = False
    empty_retried = False
    # The tool_choice to put back after a stall re-ask that forced one. See below.
    forced_from = None
    # (answer, task): the answer's check, started beside the stall check. See below.
    speculative: tuple | None = None

    def giving_up() -> bool:
        return stop_retrying is not None and stop_retrying()

    def time_up_answer() -> list:
        text = time_up_text() if time_up_text is not None else graph.TIME_UP_PARTIAL
        return [builder.text(text), builder.finish("stop", None)]

    # Whether this request has had a gateway failure: its retries are evidence about it, not
    # about the upstream (throttle.REQUEST_KIND).
    failed_once = False
    while True:
        attempt = 0
        while True:
            emitted = False
            pending = []
            # Everything this response said, streamed or held. The stall and refusal
            # check reads it: with nothing buffered (AGENTAUS_SELF_REVIEW=false) `pending`
            # stays empty, and an empty reply is never judged - so "Let me look at the
            # settings next." ended a turn unchallenged (measured: codebase_qa, 0.00).
            said = []
            finish_reason = None
            usage = None
            accumulator = ToolCallAccumulator()
            retry_reason: str | None = None
            attempt_started = time.monotonic()
            throttle.REQUEST_KIND.set(
                "retry" if failed_once and request_kind == "normal" else request_kind)

            try:
                if payload.get("stream"):
                    async with _watching_for_stall("upstream stream"):
                        opening = client.stream("POST", settings.agentaus_url,
                                                json=_on_the_wire(payload),
                                                headers=_agentaus_headers())
                        box: dict = {}
                        # Waiting for the first byte - the throttle's queue, then up to 125 s
                        # before a 524 - is the long silence the heartbeat is for.
                        async for beat in _waiting(opening.__aenter__(), builder, box):
                            yield beat
                        upstream = box["value"]
                        try:
                            if upstream.status_code >= 400:
                                detail = (await upstream.aread()).decode("utf-8", "replace")[:500]
                                rlog(logging.WARNING,
                                     "agentaus rejected the streamed request: HTTP %d %s",
                                     upstream.status_code, detail)
                                if (
                                    _is_over_length(detail)
                                    and refit is not None
                                    and fit_attempt < settings.agentaus_fit_attempts
                                ):
                                    # Nothing has been emitted yet, so the request can be
                                    # recompacted and replayed invisibly.
                                    _learn_limit_from(detail)
                                    fit_attempt += 1
                                    fit_scale *= settings.agentaus_fit_shrink
                                    payload = await refit(fit_scale)
                                    retry_reason = f"prompt too long, refitting to {fit_scale:.0%}"
                                elif upstream.status_code in _RETRYABLE_STATUS and giving_up():
                                    # A time-up turn, or an agent now past its limit: the same
                                    # prompt would fail the same way. See throttle.REQUEST_KIND.
                                    rlog(logging.WARNING, "HTTP %d on a graph agent past its "
                                         "limit; not replaying it", upstream.status_code)
                                    for piece in time_up_answer():
                                        yield piece
                                    return
                                elif (
                                    upstream.status_code in _RETRYABLE_STATUS
                                    and attempt < settings.max_retries
                                ):
                                    failed_once = True
                                    retry_reason = f"HTTP {upstream.status_code}"
                                else:
                                    yield builder.error(
                                        f"Agentaus returned HTTP {upstream.status_code}: {detail}",
                                        _error_type_for_status(upstream.status_code),
                                    )
                                    yield builder.finish("stop", None)
                                    return
                            else:
                                async for kind, line in _lines_beating(upstream, builder):
                                    if kind == "beat":
                                        yield line
                                        continue
                                    line = line.strip()
                                    if not line or not line.startswith("data:"):
                                        continue
                                    data = line[5:].strip()
                                    if data == "[DONE]":
                                        break
                                    try:
                                        chunk = json.loads(data)
                                    except json.JSONDecodeError:
                                        continue

                                    # Agentaus reports some failures - an over-length
                                    # prompt among them - as HTTP 200 with an error object
                                    # inside the SSE body. Nothing here has "choices", so
                                    # without this branch the error is skipped and the turn
                                    # ends as an empty, successful-looking message.
                                    if isinstance(chunk.get("error"), dict):
                                        message = str(chunk["error"].get("message") or "")
                                        # Nothing final has reached the client yet, so both
                                        # of these can be replayed invisibly.
                                        if not emitted and _is_transient(message) and giving_up():
                                            for piece in time_up_answer():
                                                yield piece
                                            return
                                        if (not emitted and _is_transient(message)
                                                and attempt < settings.max_retries):
                                            rlog(logging.WARNING, "agentaus in-band transient "
                                                 "error, retrying: %s", message[:200])
                                            failed_once = True
                                            retry_reason = f"upstream dropped ({message[:60]})"
                                            break
                                        if (not emitted and _is_over_length(message)
                                                and refit is not None
                                                and fit_attempt < settings.agentaus_fit_attempts):
                                            _learn_limit_from(message)
                                            fit_attempt += 1
                                            fit_scale *= settings.agentaus_fit_shrink
                                            payload = await refit(fit_scale)
                                            retry_reason = (f"prompt too long (in-band), "
                                                            f"refitting to {fit_scale:.0%}")
                                            break
                                        yield builder.error(
                                            _agentaus_error_text(chunk["error"]),
                                            chunk["error"].get("type") or "api_error",
                                        )
                                        yield builder.finish("stop", None)
                                        rlog(logging.WARNING, 
                                            "agentaus in-band error: %s",
                                            _agentaus_error_text(chunk["error"]),
                                        )
                                        return

                                    for choice in chunk.get("choices") or []:
                                        delta = choice.get("delta") or {}
                                        text = delta.get("content")
                                        if text:
                                            said.append(text)
                                            if buffering:
                                                # Held back rather than streamed: an answer
                                                # already on screen cannot be revised. Its
                                                # tokens are still shown live, as a draft.
                                                pending.append(text)
                                                live_bytes = draft(text)
                                                if live_bytes:
                                                    yield live_bytes
                                            else:
                                                emitted = True
                                                yield builder.text(text)
                                        if delta.get("tool_calls"):
                                            emitted = True
                                            accumulator.add(delta["tool_calls"])
                                        if choice.get("finish_reason"):
                                            finish_reason = choice["finish_reason"]
                                    if chunk.get("usage"):
                                        usage = chunk["usage"]
                        finally:
                            await opening.__aexit__(None, None, None)
                else:
                    box = {}
                    async for beat in _waiting(upstream_http._post_with_retry(
                        client,
                        settings.agentaus_url,
                        json_body=_on_the_wire({**payload, "stream": False}),
                        headers=_agentaus_headers(),
                        kind=request_kind,
                        give_up=stop_retrying,
                    ), builder, box):
                        yield beat
                    upstream = box["value"]
                    if upstream.status_code >= 400 and (
                            upstream.status_code in _RETRYABLE_STATUS and giving_up()):
                        for piece in time_up_answer():
                            yield piece
                        return
                    if upstream.status_code >= 400:
                        yield builder.error(
                            f"Agentaus returned HTTP {upstream.status_code}: {upstream.text[:500]}",
                            _error_type_for_status(upstream.status_code),
                        )
                        yield builder.finish("stop", None)
                        return
                    data = upstream.json()
                    if isinstance(data.get("error"), dict):
                        yield builder.error(
                            _agentaus_error_text(data["error"]),
                            data["error"].get("type") or "api_error",
                        )
                        yield builder.finish("stop", None)
                        return
                    choice = (data.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    if message.get("content"):
                        said.append(message["content"])
                        if buffering:
                            pending.append(message["content"])
                            live_bytes = draft(message["content"])
                            if live_bytes:
                                yield live_bytes
                        else:
                            emitted = True
                            yield builder.text(message["content"])
                    accumulator.add(
                        [{"index": i, **call} for i, call in enumerate(message.get("tool_calls") or [])]
                    )
                    finish_reason = choice.get("finish_reason")
                    usage = data.get("usage")

            except httpx.HTTPError as exc:
                if not emitted and _is_retryable_exception(exc) and giving_up():
                    rlog(logging.WARNING, "agentaus stream failed on a graph agent past its "
                         "limit (%s); not replaying it", _describe(exc))
                    for piece in time_up_answer():
                        yield piece
                    return
                failed_once = True
                if emitted or not _is_retryable_exception(exc) or attempt >= settings.max_retries:
                    rlog(logging.WARNING, "agentaus stream failed: %s", exc)
                    yield builder.error(f"Agentaus request failed: {exc}", "api_error")
                    yield builder.finish("stop", None)
                    return
                retry_reason = _describe(exc)

            if retry_reason:
                if live_started:
                    yield progress(f"retrying: {retry_reason}")
                await _sleep_before_retry(
                    attempt, retry_reason, time.monotonic() - attempt_started)
                attempt += 1
                continue
            break

        calls = accumulator.drain()
        if not calls and pending:
            cleaned, recovered = harmony.extract("".join(pending))
            if recovered:
                pending = [cleaned] if cleaned.strip() else []
                calls = recovered
        if settings.agentaus_batch_tools:
            calls = batching.expand_calls(calls)
        if forced_from is not None:
            payload = {k: v for k, v in payload.items() if k != "tool_choice"}
            if forced_from != "(none)":
                payload["tool_choice"] = forced_from
            forced_from = None
        known = _known_tool_names(payload)
        mine, theirs, invented = _partition_tool_calls(calls, known, library)
        if invented and corrections < settings.agentaus_correction_rounds:
            # Correct it and ask again rather than failing a tool the client cannot run.
            # Deliberately NOT charged to tool_round: a correction is the bridge fixing
            # its own upstream's mistake, and spending the budget for real work on it
            # is what made a live turn run out before it could answer.
            corrections += 1
            rlog(logging.WARNING, "model invented %d tool name(s): %s; correcting",
                 len(invented), ", ".join(c["name"] for c in invented))
            rlog(logging.INFO, "invented call(s) as sent: %s",
                 "; ".join(f"{c['name']}({str(c.get('arguments'))[:200]})" for c in invented))
            yield progress("correcting a call to a tool that does not exist")
            # The valid siblings of a batch are answered "not run" rather than dropped
            # from history - see batching.answer_batch.
            payload = _with_tool_results(payload, *batching.answer_batch(
                calls, [(c, batching.correction_for(c) or _correction_for(c, known))
                        for c in invented]))
            pending = []
            continue
        if invented:
            rlog(logging.WARNING, "dropping %d invented tool call(s) - out of rounds",
                 len(invented))
        # A well-named call with malformed arguments is rejected by the client with a
        # wall of validator internals, which costs the turn and tells the model nothing.
        # Same budget as an invented name: it is the same class of mistake.
        # The guards and the graph's task read the body as the client sent it, not the
        # compacted one - see _graph_reruns.
        reruns = _graph_reruns(theirs, received)
        await asyncio.to_thread(_run_graph_instead,
                                [c for c in theirs if all(c is not r for r, _ in reruns)],
                                received)
        if plain_text and graph.plain_text_writes(theirs):
            rlog(logging.INFO, "plain text: normalised characters in a text-file write")
        cwd = working_directory_of(received)
        broken = _validate_tool_calls(mine + theirs, _tool_schemas(payload), cwd)
        broken += reruns
        broken += early_deliverable_writes(theirs, received, cwd)
        if _graph_main_loop(received):
            broken += writes_before_workflow(theirs, received, cwd)
        broken += graph.guarded_writes(theirs, received, cwd)
        if broken and corrections < settings.agentaus_correction_rounds:
            corrections += 1
            rlog(logging.WARNING, "%d malformed tool call(s): %s; correcting",
                 len(broken), "; ".join(m for _, m in broken)[:300])
            rlog(logging.INFO, "malformed call(s) as sent: %s",
                 "; ".join(f"{c['name']}({str(c.get('arguments'))[:200]})" for c, _ in broken))
            payload = _with_tool_results(
                payload, *batching.answer_batch(mine + theirs, broken))
            pending = []
            continue
        if broken:
            rlog(logging.WARNING, "forwarding %d malformed tool call(s) - out of rounds",
                 len(broken))
        _guard_imports(theirs, received, cwd)
        if not mine:
            theirs = _reads_first(theirs, received)
        if mine and tool_round < settings.agentaus_tool_rounds:
            tool_round += 1
            rlog(logging.INFO, "running %d bridge tool call(s): %s",
                 len(mine), ", ".join(c["name"] for c in mine))
            yield progress("running " + ", ".join(c["name"] for c in mine))
            box = {}
            async for beat in _waiting(tool_handling._run_bridge_tools(
                    client, payload, mine, working_directory_of(original), ran), builder, box):
                yield beat
            payload = box["value"]
            payload = _with_unrun_calls(payload, theirs)
            # Whatever the model said on its way to calling the tool is superseded by
            # the answer it is about to give with the result in hand, so it is dropped
            # rather than shown - otherwise the user reads "let me search..." followed
            # by a search they never saw happen.
            if pending:
                rlog(logging.DEBUG, "discarded %d chars of interim narration",
                     len("".join(pending)))
            pending = []
            continue
        if mine and not theirs and not cut_off:
            # Out of rounds with a search still wanted. Withdraw the search tools but not
            # Claude Code's, so a next step that is not a search can still be taken.
            cut_off = True
            rlog(logging.WARNING,
                 "tool round limit (%d) reached; dropping %d unanswered bridge call(s) "
                 "and asking again without the bridge's tools",
                 settings.agentaus_tool_rounds, len(mine))
            yield progress("search budget spent; asking for the answer")
            payload = _without_bridge_tools(payload)
            pending = []
            continue
        # An empty reply is never a valid answer, but it is not a reason to take the tools
        # away either: the first one is sent again as it was.
        if not calls and not emitted and not "".join(pending).strip() and not empty_retried:
            empty_retried = True
            rlog(logging.WARNING, "upstream returned an empty answer; asking again with "
                 "the same tools")
            continue
        # A turn that used no tools and claims it has none is not an answer. Whether it
        # IS that is a judgement about language, so Agentaus makes it - behind a
        # structural gate, so the call only happens when there is something to judge.
        answer_so_far = "".join(pending) or "".join(said)
        is_refusal = False
        turn_verdict = "answer"
        # Files the user's request says to write that are still not on disk. A main loop
        # only: a graph agent's prompt names files the graph writes itself.
        missing = [] if (mine or theirs or not known or is_subagent_turn(original)
                         or awaiting_background(original)) else missing_deliverables(
            _last_user_text(received), working_directory_of(received))
        stalled_on_its_face, missing, announced = _structural_stall(answer_so_far, missing)
        # One gate for both checks below, so the image exemption covers the judge too.
        worth_checking = (corrections < settings.agentaus_correction_rounds
                          and stall_check._worth_checking(original, answer_so_far, tools_offered=bool(known),
                                              called_a_tool=bool(mine or theirs)))
        if worth_checking and stalled_on_its_face:
            # It says what it will do next, or left out the files: no judge call to wait on.
            turn_verdict = "stalled"
        elif worth_checking:
            async def classify() -> str:
                async with gate.hold("refusal check", "urgent"):
                    return await helper_calls._agentaus_summarise(
                        client, CLASSIFY_REFUSAL_INSTRUCTION.format(
                            request=_original_request(received)[:4000],
                            ran=ledger.render(original.get("messages") or [], limit=40)
                            or "(nothing yet)",
                            answer=_head_and_tail(answer_so_far, 2500, 1500))
                    )

            # The answer's own check does not depend on the verdict, so it starts now rather
            # than after it: if the verdict is "answer" (most of the time), the check is
            # already under way; if not, it is cancelled. Not when the syntax fix may still
            # change the answer.
            if speculative is not None:
                speculative[1].cancel()
                speculative = None
            guess = normalise_for_display("".join(pending))
            if (settings.agentaus_overlap_checks and buffering and guess and not theirs
                    and not (settings.agentaus_syntax_check and syntax.first_error(
                        guess, strict=mid_tool_loop(original)))):
                early = helper_calls._answer_check(client, original, guess, ran)
                if early is not None:
                    speculative = (guess, asyncio.ensure_future(early))
                    if checks is not None:
                        checks.add(speculative[1])
            try:
                box = {}
                async for beat in _waiting(classify(), builder, box):
                    yield beat
                turn_verdict = read_turn_verdict(box["value"])
                is_refusal = turn_verdict == "refusal"
            except Exception as exc:
                # Cannot classify it, so forward it. Re-asking a good answer is worse
                # than letting a bad one through: the user can repeat a turn.
                rlog(logging.WARNING, "refusal check failed (%s); forwarding the answer",
                     exc)

        if turn_verdict == "stalled" and not _may_reask_stalled(
                original, announced=announced):
            rlog(logging.WARNING, "judged stalled again, but the re-ask budget for this "
                 "message is spent; forwarding the reply")
            turn_verdict = "answer"
        if is_refusal or turn_verdict == "stalled":
            if speculative is not None:
                speculative[1].cancel()
                speculative = None
            corrections += 1
            if is_refusal:
                rlog(logging.WARNING,
                     "model refused to use tools it has; correcting and re-asking")
            elif missing:
                rlog(logging.WARNING, "turn ended without the file(s) the request names "
                     "(%s); asking for them", ", ".join(n for n, _ in missing))
            else:
                rlog(logging.WARNING,
                     "model announced a step and stopped; asking it to take the step")
            yield progress("re-asking: the reply " + (
                "declined to use its tools" if is_refusal
                else "left out the file(s) the request asks for" if missing
                else "announced a step it did not take"))
            correction = (REFUSAL_CORRECTION.format() if is_refusal
                          else MISSING_DELIVERABLES_CORRECTION.format(files="\n".join(
                              f"- `{name}` (looked for `{path}`)" for name, path in missing))
                          if missing else STALLED_CORRECTION.format())
            payload = {**payload, "messages": list(payload.get("messages") or []) + [
                {"role": "assistant", "content": answer_so_far[:2000]},
                {"role": "user", "content": correction},
            ]}
            if not is_refusal and payload.get("tools"):
                # Words alone kept failing: "We will perform the edits." ended two runs
                # after both re-asks were spent on more of the same. `required` makes the
                # call the step itself - honoured about one time in three when measured,
                # and harmless when ignored. For this one call only.
                forced_from = payload.get("tool_choice", "(none)")
                payload = {**payload, "tool_choice": "required"}
            pending = []
            continue

        # The turn is about to end on prose. Structural checks, no model call to decide:
        # a requested file that does not exist, code changed and never run since, a test
        # run that still fails, or (once) the request's own checklist. See finish.py.
        if (settings.agentaus_finish_gate and buffering and not mine and not theirs
                and corrections < settings.agentaus_correction_rounds
                and payload.get("tools") and not is_subagent_turn(original)
                and not graph.is_graph_task(original)
                and not awaiting_background(original)):
            try:
                gate_kind, gate_text, gate_force = finish.finish_gate(
                    _stall_key(received), _original_request(received),
                    list(received.get("messages") or []), working_directory_of(received),
                    can_run="Bash" in known, checklist_on=settings.agentaus_finish_checklist)
            except Exception as exc:  # noqa: BLE001 - a check must never break a turn
                rlog(logging.WARNING, "finish gate failed (%s); forwarding the answer", exc)
                gate_kind = ""
            if gate_kind:
                corrections += 1
                rlog(logging.WARNING, "finish gate: %s; re-asking before the turn ends",
                     gate_kind)
                yield progress("re-asking: " + {
                    "deliverable": "the requested file has not been written",
                    "unverified": "the changed code has not been run",
                    "failed": "the last test run still fails",
                    "checklist": "checking the work against the request",
                }.get(gate_kind, gate_kind))
                payload = {**payload, "messages": list(payload.get("messages") or []) + [
                    {"role": "assistant", "content": "".join(pending)[:2000] or "Done."},
                    {"role": "user", "content": gate_text},
                ]}
                if gate_force:
                    forced_from = payload.get("tool_choice", "(none)")
                    payload = {**payload, "tool_choice": "required"}
                # The overlapped check was checking the answer this re-ask replaces.
                if speculative is not None:
                    speculative[1].cancel()
                    speculative = None
                pending = []
                continue

        if mine:
            # Out of rounds. These must not be emitted: Claude Code has never heard of
            # `agentaus_search` and would fail the tool_use rather than run it.
            rlog(logging.WARNING,
                 "tool round limit (%d) reached; dropping %d unanswered bridge call(s)",
                 settings.agentaus_tool_rounds, len(mine))
            if buffering and pending and not theirs:
                # Narration before a search that will not run promises work that never
                # happens. The forced answer replaces it, as it does unbuffered.
                rlog(logging.DEBUG, "discarded %d chars of interim narration",
                     len("".join(pending)))
                pending = []
            if not pending and not theirs:
                # Every one of those searches is still in the payload. Asking again with
                # the tools REMOVED forces the model to answer from what it gathered,
                # which is strictly better than apologising for having gathered it.
                # Observed: a locatable symbol spent twelve rounds being searched for and
                # the turn ended "I ran out of search rounds", with the answer sitting in
                # the conversation the whole time.
                box = {}
                async for beat in _waiting(_answer_without_tools(client, payload), builder, box):
                    yield beat
                forced = box["value"]
                pending = [forced] if forced else [
                    "I searched without reaching an answer. Narrow the question and I "
                    "will try again."
                ]
                if not buffering:
                    # Only a buffered answer is emitted below; a live one is sent here.
                    yield builder.text(pending[0])
            if not theirs:
                # The dropped search left "tool_calls" here, and the turn now has none -
                # also when the model narrated before searching again after the cut-off.
                finish_reason = "stop"
        break

    # Same repair as the buffered path: a non-breaking hyphen in a filename looks
    # exactly like a hyphen and cannot be copied, clicked or searched for.
    answer = normalise_for_display("".join(pending))
    if buffering and not answer and not theirs and not accumulator.pending():
        # Agentaus sometimes answers HTTP 200 with no content at all - observed as
        # "200 in 94.8s in=0 out=0" on a question it had every means to answer. An empty
        # reply is never right, so ask once more before forwarding silence.
        rlog(logging.WARNING, "upstream returned an empty answer; asking once more")
        box = {}
        async for beat in _waiting(_answer_without_tools(client, payload), builder, box):
            yield beat
        retried = box["value"]
        if retried:
            answer = retried
            pending = [answer]

    if (buffering and answer and settings.agentaus_syntax_check and not theirs
            and not is_subagent_turn(original)):
        box = {}
        fixing = _fix_syntax(client, answer, strict=mid_tool_loop(original))
        async for beat in _waiting(fixing, builder, box):
            yield beat
        answer = box["value"]
        pending = [answer]

    if speculative is not None and (speculative[0] != answer or theirs or not buffering):
        speculative[1].cancel()
        speculative = None
    if buffering and answer:
        if live_started and not theirs:
            yield progress("checking the answer")
        check = None
        if speculative is not None and speculative[0] == answer and not theirs:
            # Already running since the stall check started, on this very answer.
            check, speculative = speculative[1], None
        elif not theirs:
            check = helper_calls._answer_check(client, original, answer, ran)
        if check is not None:
            box = {}
            async for beat in _waiting(check, builder, box):
                yield beat
            answer = box["value"]
        yield builder.text(answer)

    for call in theirs:
        yield builder.tool_use(call["id"], call["name"], call["arguments"])

    _calibrate_from_usage(original, usage)
    yield builder.finish(finish_reason, usage)
    rlog(logging.INFO, 
        "POST /v1/messages model=%s route=agentaus stream=true -> 200 in %.1fs finish=%s usage=%s",
        model,
        time.monotonic() - started,
        finish_reason,
        usage,
    )


async def _agentaus_event_stream(*args, **kwargs) -> AsyncIterator[bytes]:
    """`_unguarded_event_stream`, with the answer check it may start early (the overlapped
    check, AGENTAUS_OVERLAP_CHECKS) cancelled however the stream ends. That check is a task
    of its own, so a client that disconnects mid-turn - which closes this generator at
    whichever `yield` it had reached - would otherwise leave it running: an upstream call
    and a gate slot spent on an answer nobody will read. A wrapper rather than a
    try/finally inside, so the stream's own code and output are untouched."""
    checks: set = set()
    stream = _unguarded_event_stream(*args, checks=checks, **kwargs)
    try:
        async for piece in stream:
            yield piece
    finally:
        for task in checks:
            task.cancel()
        await stream.aclose()

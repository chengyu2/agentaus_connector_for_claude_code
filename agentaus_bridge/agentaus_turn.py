"""One Agentaus turn from request to response, buffered or streamed."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from typing import AsyncIterator

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import agentaus_stream
from . import attribution
from . import batching
from . import context_window
from . import diet
from . import documents
from . import finish
from . import flight
from . import graph
from . import helper_calls
from . import import_guard
from . import name_check
from . import persisted
from . import plans
from . import read_first
from . import repair
from . import sessions
from . import tool_handling
from . import tools as bridge_tools
from . import upstream_http

from .augment import (
    early_deliverable_writes,
    focus_tools,
    is_subagent_turn,
    mid_tool_loop,
    should_think,
    with_guidance,
    with_library_skills,
    with_parent_request,
    with_plan,
    with_skill_descriptions,
    workflow_opted_in,
    working_directory_of,
    worth_reviewing,
    worth_reviewing_turn,
    writes_before_workflow,
)
from .bridge_responses import (
    _error_response,
    _exact_command_response,
    _synthetic_response,
    _upstream_error_response,
)
from .config import settings
from .context_window import _calibrate_from_usage, _get_distiller
from .conversation_text import _last_user_text, _original_request, _user_texts
from .graph_turn import (
    _graph_launch,
    _graph_main_loop,
    _graph_not_started,
    _graph_reruns,
    _graph_session,
    _graph_waiting_reply,
    _run_graph_instead,
    _without_graph_script,
    _writes_plain_text,
)
from .helper_calls import (
    _answer_without_tools,
    _check_grounding,
    _checks_apply,
    _fix_syntax,
    _grounding_applies,
    _without_bridge_tools,
)
from .ledger import with_ledger
from .request_log import _Phase, rlog
from .stream_delivery import _answer_buffered, _heartbeat, _keepalive, _relay, _waiting
from .tool_handling import (
    _correction_for,
    _guard_imports,
    _known_tool_names,
    _on_the_wire,
    _openai_calls,
    _partition_tool_calls,
    _recover_harmony_calls,
    _tool_schemas,
    _validate_tool_calls,
    _with_coerced_calls,
    _with_forced_answer,
    _with_reads_first,
    _with_tool_results,
    _with_unrun_calls,
    _without_bridge_calls,
    _without_unknown_calls,
)
from .translate import (
    AnthropicStreamBuilder,
    agentaus_response_to_anthropic,
    anthropic_request_to_agentaus,
    estimate_request_tokens,
    inject_bridge_tools,
)
from .upstream_http import (
    _RETRYABLE_STATUS,
    _agentaus_error_text,
    _agentaus_headers,
    _context_limit,
    _is_over_length,
    _is_too_slow,
    _learn_limit_from,
    _worth_refitting,
)


async def _handle_agentaus(
    request: Request, body: dict, model: str, wants_stream: bool
) -> Response:
    client: httpx.AsyncClient = request.app.state.client
    started = time.monotonic()
    display_model = model or "agentaus"

    # Fit the conversation to the window, then send it. Deliberately NOT a hard
    # pre-flight rejection on our own arithmetic: our token count is an estimate, and
    # refusing a request the model would have accepted is a brittle way to fail. The
    # API is the authority - if Agentaus says a prompt is too long it also states its
    # real limit, which is better evidence than anything computed here. So the estimate
    # only decides *when to compact*, a recoverable choice, and an actual rejection
    # drives a tighter retry rather than ending the turn.
    limit = _context_limit()
    reserved = int(body.get("max_tokens") or 0)
    original_estimate = estimate_request_tokens(body)
    # The body as the client sent it. `body` is rebound to the fitted one below, and a
    # compacted body has lost the turns the guards and the graph's task are read from.
    received = body
    # When the client finished reading or writing each file, for read_first. Before anything
    # below can touch the files.
    read_first.observe(body.get("messages") or [])

    # An agent that runs one exact command is played by the bridge. See graph.exact_command.
    exact = graph.exact_command(body) if is_subagent_turn(body) else None
    if exact is not None:
        return _exact_command_response(body, exact, display_model, wants_stream, started)
    if _graph_main_loop(body):
        # Fold this request's launches and reports into the session's graph record first.
        _graph_session(body)
        launch = _graph_launch(body)
        if launch:
            rlog(logging.INFO, "graph main loop: no workflow has run; launching the agent "
                 "graph without the model")
            return _synthetic_response(body, display_model, wants_stream, started,
                                       "graph launch", tool=("Workflow", launch))
        waiting = _graph_waiting_reply(body)
        if waiting:
            rlog(logging.INFO, "graph main loop: the workflow is launched; replying without "
                 "the model")
            return _synthetic_response(body, display_model, wants_stream, started,
                                       "workflow launched", text=waiting)

    # How far past its time a graph agent is, read off the request as received: fitting and
    # compaction rewrite the messages the agent is known by. See graph.overdue.
    clock = {"over": 0.0}
    graph_agent = graph.is_graph_task(body) and is_subagent_turn(body)
    if graph_agent:
        over, news = graph.overdue(body)
        clock["over"] = over
        if over > settings.agentaus_graph_give_up_s:
            # The graph has stopped waiting for this agent, and the agent still holds a slot in
            # the harness. Measured (d13, d14): stuck agents ran 68-81 min before Claude Code
            # failed them; one request was retried for 2,627 s. The graph reads this short reply
            # as no answer.
            rlog(logging.WARNING, "graph agent (%s) is %.0fs past its %ds limit; giving up on "
                 "it without an upstream call", graph.role_of(body) or "?", over,
                 graph.agent_limit(body))
            return _synthetic_response(body, display_model, wants_stream, started,
                                       "graph agent past its limit", text=graph.OUT_OF_TIME)
        if over and news:
            rlog(logging.WARNING, "graph agent (%s) is %.0fs past its %ds limit; "
                 "asking for its result with no tools", graph.role_of(body) or "?",
                 over, graph.agent_limit(body))

    def stop_retrying() -> bool:
        """Whether a gateway failure on this turn is answered by the bridge, not replayed: a
        time-up turn (its 524s are about the request - see throttle.REQUEST_KIND), or a graph
        agent that has run past its limit while its request was being retried."""
        return graph_agent and (clock["over"] > 0 or graph.seconds_over(received) > 0)

    def time_up_text() -> str:
        rlog(logging.WARNING, "graph agent (%s): a gateway failure past its limit; answering "
             "with what it already has", graph.role_of(received) or "?")
        return graph.time_up_fallback(received)

    request_kind = "time_up" if clock["over"] else "normal"
    # Decided on the request as received, before compaction can drop the opt-in.
    plain_text = _writes_plain_text(body)

    # Written once and reused across refits: a refit recompacts the same turn, and
    # re-planning it would pay for the same call again to get the same plan.
    plan_holder: dict = {"text": None}
    # Which turn this request belongs to, for a plan written in the background (plans.py).
    turn_key = plans.turn_key(sessions.session_of(request.headers), _user_texts(received))
    # The library skills prepare() offered - the calls the bridge must answer itself.
    library_holder: dict = {"names": frozenset()}

    async def prepare(
        current: dict, scale: float = 1.0, on_plan_piece=None, on_note=None
    ) -> tuple[dict, dict]:
        """Fit to the window, add the guidance and the plan, and build the payload.

        Returns (body, payload). Compaction can take a minute or more on a long
        conversation, which is why the streaming path runs this *inside* the response
        generator rather than before it - see the note there.
        """
        # A `.docx` read by Claude Code's own Read tool arrives as zip noise, because
        # that tool runs on the client and a `.docx` is a zip. Fix it before anything
        # else looks at it - distillation would otherwise spend model calls condensing
        # binary, and the model would answer from nothing.
        #
        # Both of these read files - the first may run LibreOffice - so they run in a
        # worker thread. On the event loop they would stall every other request.
        if settings.agentaus_office_extract:
            current = await asyncio.to_thread(documents.repair_tool_results, current)

        # A preview standing in for a 1.6 MB listing is worse than useless - the model
        # answers from a fragment and fills the rest in. The client saved the real output
        # to disk and the bridge is on the same machine, so read it.
        current = await asyncio.to_thread(
            persisted.restore, current,
            int(limit * settings.agentaus_restore_window_share) if limit > 0 else None)

        # An Edit refused as "not found" gets the closest real lines, indentation exact, so the
        # next try copies them instead of re-reading. See repair.edit_hints.
        if settings.agentaus_edit_repair:
            current, hinted = await asyncio.to_thread(repair.edit_hints, current)
            if hinted:
                rlog(logging.INFO, "edit repair: showed the closest lines for %d refused "
                     "Edit(s)", hinted)
        # A .py edit that left a name undefined in a function it touched is said so in its
        # result. See name_check.py.
        if settings.agentaus_name_check:
            current, flagged = await asyncio.to_thread(name_check.annotate, current)
            if flagged:
                rlog(logging.INFO, "name check: %d edit(s) left a name undefined", flagged)
        # A graph main loop's .py edit that broke importing the module is said so in its
        # result. See import_guard.py.
        if settings.agentaus_import_guard and _graph_main_loop(current):
            current, broke = await asyncio.to_thread(import_guard.check, current)
            if broke:
                rlog(logging.WARNING, "import guard: %d edit(s) broke a module's import", broke)

        # The graph script the bridge put in a Workflow call is ~115 KB, resent upstream on
        # every later main-loop request. The model never needs it again.
        if not is_subagent_turn(current):
            current, swapped = _without_graph_script(current)
            if swapped:
                rlog(logging.INFO, "workflow: sent %d graph call(s) upstream without the script",
                     swapped)

        # Claude Code asks every model it does not know to credit Claude in its commits.
        current, credited = attribution.credit(current, settings.agentaus_coauthor)
        if credited:
            rlog(logging.INFO, "attribution: commits credit %s", settings.agentaus_coauthor)

        # Claude Code lists project skills by bare name; put back what each is for, so
        # the model - and the planner, which reads the same listing - can choose one.
        # Before fitting, so the window estimate counts the descriptions.
        if settings.agentaus_skill_routing:
            current, described = with_skill_descriptions(current)
            if described:
                rlog(logging.INFO, "skill listing: described %d project skill(s)", described)
        if settings.agentaus_skill_library:
            current, served = with_library_skills(current)
            library_holder["names"] = served
            if served:
                rlog(logging.INFO, "skill listing: offered %d library skill(s)", len(served))

        # Condense oversized tool results BEFORE fitting. Order matters: this is what
        # decides whether compaction is needed at all, and compacting first would
        # summarise output that was about to be condensed anyway.
        if settings.agentaus_distill_results:
            raw = estimate_request_tokens(current)
            current = await _get_distiller(request).distill(current)
            shrunk = estimate_request_tokens(current)
            if shrunk < raw:
                rlog(logging.INFO, "distillation: %d -> %d est tokens", raw, shrunk)

        # The ledger is built here, from the message list as it stands *before*
        # compaction. That is the whole point: the calls that fall out of the window are
        # the ones the model forgets it already made.
        if graph.is_graph_task(current):
            current, cut = graph.trim_results(current)
            if cut:
                rlog(logging.INFO, "graph subagent: cut %d oversized tool result(s)", cut)

        ledger_source = list(current.get("messages") or [])
        # Same reason: compaction can summarise away the "ultracode: ..." that opted in.
        opted_in = workflow_opted_in(current)

        before = estimate_request_tokens(current)
        if before + reserved > int(limit * settings.agentaus_compact_threshold):
            if on_note is not None:
                on_note(f"compacting the earlier conversation to fit Agentaus' "
                        f"{limit:,}-token window (~{before:,} tokens now)")
            with _Phase("compaction", f"est {before:,} tok, target {limit:,}"):
                fitted = await context_window._fit_to_window(
                    request, current, limit=limit, reserve=reserved, scale=scale
                )
            rlog(logging.INFO, "compaction result: %d -> %d est tokens",
                 before, estimate_request_tokens(fitted))
        else:
            fitted = await context_window._fit_to_window(
                request, current, limit=limit, reserve=reserved, scale=scale
            )
        # Offer the bridge's own search and steer Grep towards literal lookups. Done
        # here rather than before compaction so the tool list travels with whatever
        # survived the fit; it costs ~250 tokens the estimate does not see, which is
        # immaterial against a 0.8 threshold.
        if settings.agentaus_tool_focus:
            fitted, dropped = focus_tools(fitted, workflow_opt_in=opted_in)
            if graph.is_graph_task(fitted):
                fitted, more = graph.focus(fitted)
                dropped = dropped + more
            if dropped:
                rlog(logging.INFO, "tool focus: offering %d tool(s), holding back %d (%s)",
                     len(fitted.get("tools") or []), len(dropped), ", ".join(dropped)[:200])

        offered = []
        # Inventory first in the list as well as first in the workflow: a question about
        # a whole tree should start here, and a model reads the tools in order.
        if settings.agentaus_inventory:
            offered.append(bridge_tools.INVENTORY_SCHEMA)
        if settings.agentaus_search:
            offered.append(bridge_tools.SEARCH_SCHEMA)
        if settings.agentaus_web_search:
            offered.append(bridge_tools.WEB_SEARCH_SCHEMA)
        if settings.agentaus_investigate:
            offered.append(bridge_tools.INVESTIGATE_SCHEMA)
        if settings.agentaus_zoom:
            offered.append(bridge_tools.ZOOM_SCHEMA)
        # Not for a graph subagent: the graph already chose its context, and one
        # `agentaus_search` fans out into dozens of model calls - measured, hundreds of a
        # graph run's calls against an upstream that serves about thirty a minute.
        if offered and not graph.is_graph_task(fitted):
            fitted = inject_bridge_tools(fitted, offered)

        if settings.agentaus_prompt_diet:
            fitted = diet.slim(fitted)

        if settings.agentaus_tool_ledger:
            fitted = {**fitted, "system": with_ledger(
                fitted.get("system"), ledger_source,
                limit=settings.agentaus_tool_ledger_limit,
            )}

        # Many calls in, and the file the request asked for still does not exist: say so
        # where the model reads it on every step. Measured: a Q&A turn read files one at a
        # time for ten minutes and timed out without ever writing its answers file.
        if ((settings.agentaus_deliverable_nudge_calls > 0 or settings.agentaus_rewrite_nudge)
                and fitted.get("tools")
                and not is_subagent_turn(fitted) and not graph.is_graph_task(fitted)):
            with contextlib.suppress(Exception):
                nudge = finish.deliverable_nudge(
                    _original_request(received), working_directory_of(received),
                    list(received.get("messages") or []),
                    after_calls=settings.agentaus_deliverable_nudge_calls)
                if settings.agentaus_rewrite_nudge:
                    nudge += finish.rewrite_nudge(list(received.get("messages") or []),
                                                  working_directory_of(received))
                if nudge:
                    system = fitted.get("system")
                    if isinstance(system, list):
                        system = list(system) + [{"type": "text", "text": nudge.strip()}]
                    else:
                        system = (system or "") + "\n" + nudge
                    fitted = {**fitted, "system": system}
                    rlog(logging.INFO, "finish nudge: %d chars", len(nudge))

        # Supplement the system prompt for Agentaus only. Claude turns never reach
        # here - they are forwarded untouched by _passthrough.
        if settings.agentaus_guidance:
            fitted = {**fitted, "system": with_guidance(
                fitted.get("system"), fitted, routing=settings.agentaus_skill_routing,
                workflow_opt_in=opted_in)}

        # A graph subagent's prompt already carries exactly the context the graph chose;
        # the whole request on top would undo the point of a small prompt.
        if is_subagent_turn(fitted) and not graph.is_graph_task(fitted):
            parent = sessions.request_of(sessions.session_of(request.headers))
            if parent:
                fitted = {**fitted, "system": with_parent_request(fitted.get("system"), parent)}
                rlog(logging.INFO, "subagent: shown its session's request (%d chars)",
                     len(parent))

        # Measured (d12 c1913): the planning pass took 254 s and failed with 524 before a
        # graph main loop's first turn reached the model - a turn whose job is to call Workflow.
        thinking_on = settings.agentaus_thinking and not _graph_not_started(received)
        if thinking_on and settings.agentaus_plan_async and turn_key is not None:
            # Planned beside the first call rather than before it; every later request of
            # the turn carries the plan once it is written. See plans.py.
            if should_think(fitted):
                planned_body = fitted
                if plans.start(turn_key,
                               lambda: helper_calls._plan_turn(client, planned_body, priority="normal"),
                               session=sessions.session_of(request.headers)):
                    rlog(logging.INFO, "planning in the background; first call goes unplanned")
            elif mid_tool_loop(fitted) and not is_subagent_turn(fitted):
                waited = await plans.wait_once(turn_key, settings.agentaus_plan_wait_seconds)
                if waited:
                    rlog(logging.INFO, "waited %.1fs for the plan before the turn's second call",
                         waited)
                found = plans.ready(turn_key)
                if found is not None:
                    text, first = found
                    fitted = {**fitted, "system": with_plan(fitted.get("system"), text,
                                                            mid_turn=True)}
                    if first:
                        # Shown to the user once, the first time a call follows it.
                        plan_holder["text"] = text
                        rlog(logging.INFO, "plan joined the turn (%d chars)", len(text))
                elif plans.pending(turn_key):
                    rlog(logging.INFO, "plan not written yet; this call goes without it")
        elif thinking_on and should_think(fitted):
            if plan_holder["text"] is None:
                with _Phase("planning"):
                    plan_holder["text"] = await helper_calls._plan_turn(client, fitted, on_plan_piece)
            fitted = {**fitted, "system": with_plan(fitted.get("system"), plan_holder["text"])}

        # A graph agent past its time is asked for its result, with no tools to keep going.
        # Last, so nothing after it puts tools back. The clock was read off the request as
        # received, above. See graph.overdue.
        if clock["over"]:
            fitted = graph.time_up(fitted)

        built = anthropic_request_to_agentaus(
            fitted,
            system_prompt_overwrite=settings.system_prompt_overwrite,
            # Streamed upstream whatever the client asked: see config.upstream_stream.
            stream=settings.upstream_stream,
        )
        if settings.log_bodies:
            rlog(logging.INFO, "-> agentaus payload: %s", json.dumps(built)[: settings.log_body_chars])
        return fitted, built

    async def run_buffered() -> Response:
        nonlocal body
        body, payload = await prepare(body)
        scale = 1.0
        data: dict = {}

        # Rounds of bridge-executed tool calls. Each round runs the tool, appends its
        # result, and asks Agentaus again. The loop ends when the model answers with
        # prose, or with a tool only Claude Code can run.
        corrections = 0
        ran: dict = {}
        # Each re-ask below happens once: the bridge's tools withdrawn at the round limit,
        # and an empty reply sent again unchanged.
        cut_off = False
        empty_retried = False
        # Charged only for rounds that run bridge tools, as on the streamed path: a
        # correction or a re-ask is not work the budget is for.
        tool_round = 0
        # Unbounded on its face, bounded by what continues it: corrections, bridge rounds
        # and the two re-asks above each have their own limit.
        for _ in itertools.count():
            # Agentaus is the authority on whether a prompt fits. If it says no, it also
            # says what its real limit is, so the next attempt compacts against that
            # instead of against our estimate.
            for fit_attempt in range(settings.agentaus_fit_attempts + 1):
                try:
                    upstream = await upstream_http._post_with_retry(
                        client,
                        settings.agentaus_url,
                        json_body=_on_the_wire(payload),
                        headers=_agentaus_headers(),
                        stream_upstream=bool(payload.get("stream")),
                        kind=request_kind,
                        give_up=stop_retrying,
                    )
                except httpx.HTTPError as exc:
                    if stop_retrying():
                        return _synthetic_response(body, display_model, False, started,
                                                   "time up", text=time_up_text())
                    return _error_response(502, "api_error", f"Agentaus request failed: {exc}")

                # A timeout is treated like an over-length rejection: both mean the
                # next attempt must send less, and neither is fixed by replaying.
                over_length = upstream.status_code >= 400 and (
                    _is_over_length(upstream.text)
                    or (_is_too_slow(upstream.text) and _worth_refitting(body, limit))
                )
                if not over_length or fit_attempt == settings.agentaus_fit_attempts:
                    break

                _learn_limit_from(upstream.text)
                scale *= settings.agentaus_fit_shrink
                rlog(logging.WARNING,
                    "Agentaus rejected the prompt as too long; recompacting to %.0f%% "
                    "of the window and retrying", scale * 100,
                )
                body = await context_window._fit_to_window(
                    request, body, limit=_context_limit(), reserve=reserved, scale=scale
                )
                payload = anthropic_request_to_agentaus(
                    body,
                    system_prompt_overwrite=settings.system_prompt_overwrite,
                    stream=settings.upstream_stream,
                )

            if upstream.status_code >= 400:
                if upstream.status_code in _RETRYABLE_STATUS and stop_retrying():
                    return _synthetic_response(body, display_model, False, started,
                                               "time up", text=time_up_text())
                return _upstream_error_response(upstream.status_code, upstream.text)

            try:
                data = upstream.json()
            except ValueError:
                return _error_response(502, "api_error", "Agentaus returned a non-JSON response")

            # Agentaus can answer HTTP 200 with an error object instead of choices.
            if isinstance(data.get("error"), dict):
                rlog(logging.WARNING, "agentaus in-band error: %s", _agentaus_error_text(data["error"]))
                return _error_response(
                    400,
                    data["error"].get("type") or "api_error",
                    _agentaus_error_text(data["error"]),
                )

            _calibrate_from_usage(body, data.get("usage"))

            if settings.agentaus_batch_tools:
                data = batching.expand_response(data)
            known = _known_tool_names(payload)
            calls = _openai_calls(data)
            if not calls:
                data, calls = _recover_harmony_calls(data)
            mine, _theirs, invented = _partition_tool_calls(
                calls, known, library_holder["names"])
            if invented and corrections < settings.agentaus_correction_rounds:
                corrections += 1
                rlog(logging.WARNING, "model invented %d tool name(s): %s; correcting",
                     len(invented), ", ".join(c["name"] for c in invented))
                # The valid siblings of a batch are answered "not run" rather than
                # dropped from history - see batching.answer_batch.
                payload = _with_tool_results(payload, *batching.answer_batch(
                    calls, [(c, batching.correction_for(c) or _correction_for(c, known))
                            for c in invented]))
                continue
            # The guards and the graph's task read the body as the client sent it, not the
            # compacted one - see _graph_reruns.
            reruns = _graph_reruns(_theirs, received)
            # In a worker thread: preparing the graph copies the project and reads its
            # documents - measured, 9s for fourteen PDFs - and would stall every other request.
            await asyncio.to_thread(_run_graph_instead,
                                    [c for c in _theirs if all(c is not r for r, _ in reruns)],
                                    received)
            if plain_text and graph.plain_text_writes(_theirs):
                rlog(logging.INFO, "plain text: normalised characters in a text-file write")
            cwd = working_directory_of(received)
            broken = _validate_tool_calls(mine + _theirs, _tool_schemas(payload), cwd)
            broken += reruns
            broken += early_deliverable_writes(_theirs, received, cwd)
            if _graph_main_loop(received):
                broken += writes_before_workflow(_theirs, received, cwd)
            broken += graph.guarded_writes(_theirs, received, cwd)
            if broken and corrections < settings.agentaus_correction_rounds:
                corrections += 1
                rlog(logging.WARNING, "%d malformed tool call(s): %s; correcting",
                     len(broken), "; ".join(m for _, m in broken)[:300])
                payload = _with_tool_results(
                    payload, *batching.answer_batch(mine + _theirs, broken))
                continue
            if broken:
                rlog(logging.WARNING, "forwarding %d malformed tool call(s) - out of "
                     "rounds to correct them", len(broken))
            _guard_imports(_theirs, received, cwd)
            data = _with_coerced_calls(data, mine + _theirs)
            # An empty reply is never a valid answer, but it is not a reason to take the
            # tools away either: the first one is sent again as it was.
            if (not calls and not empty_retried and not (
                    ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                    or "").strip()):
                empty_retried = True
                rlog(logging.WARNING, "upstream returned an empty answer; asking again "
                     "with the same tools")
                continue
            if not mine:
                data = _with_reads_first(data, _theirs, received)
                if invented:
                    rlog(logging.WARNING, "dropping %d invented tool call(s) - out of "
                         "rounds to correct them", len(invented))
                    data = _without_unknown_calls(data, known)
                break
            if tool_round < settings.agentaus_tool_rounds:
                tool_round += 1
                rlog(logging.INFO, "running %d bridge tool call(s): %s",
                     len(mine), ", ".join(c["name"] for c in mine))
                payload = await tool_handling._run_bridge_tools(
                    client, payload, mine, working_directory_of(body), ran)
                payload = _with_unrun_calls(payload, _theirs)
                continue
            # Out of rounds with a bridge tool still pending. It must not reach Claude
            # Code, which has never heard of it and would fail the tool_use outright.
            rlog(logging.WARNING,
                 "tool round limit (%d) reached; dropping %d unanswered bridge call(s)",
                 settings.agentaus_tool_rounds, len(mine))
            if _theirs:
                # Claude Code still has work to run, so the turn goes on without a re-ask.
                data = _without_bridge_calls(data, drop={c["id"] for c in mine if c.get("id")})
                break
            if not cut_off:
                cut_off = True
                payload = _without_bridge_tools(payload)
                continue
            # Everything those searches found is still in the payload, so ask again with
            # the tools removed rather than surfacing an apology for having searched.
            forced = await _answer_without_tools(client, payload)
            data = _with_forced_answer(data, forced) if forced else _without_bridge_calls(
                data, drop={c["id"] for c in mine if c.get("id")})
            break

        choice0 = (data.get("choices") or [{}])[0]
        msg0 = choice0.get("message") or {}

        # An empty reply is never a valid answer to a question. Observed as
        # "200 in 94.8s in=0 out=0" on a question the model had every means to answer.
        if not (msg0.get("content") or "").strip() and not msg0.get("tool_calls"):
            rlog(logging.WARNING, "upstream returned an empty answer; asking once more")
            retried = await _answer_without_tools(client, payload)
            if retried:
                data = _with_forced_answer(data, retried)
                choice0 = (data.get("choices") or [{}])[0]
                msg0 = choice0.get("message") or {}

        if (settings.agentaus_syntax_check and not msg0.get("tool_calls")
                and not is_subagent_turn(body)
                and (msg0.get("content") or "").strip()):
            parsed = await _fix_syntax(client, msg0["content"], strict=mid_tool_loop(body))
            if parsed != msg0.get("content"):
                msg0 = {**msg0, "content": parsed}
                data = {**data, "choices": [{**choice0, "message": msg0}]
                        + list((data.get("choices") or [])[1:])}

        # Review only a plain text answer. A turn that calls tools is mid-task, and
        # rewriting it would break the tool_use the client is waiting on.
        if (
            settings.agentaus_self_review
            and _checks_apply(body)
            and not msg0.get("tool_calls")
            and not ran
            and worth_reviewing_turn(body)
            and worth_reviewing(msg0.get("content") or "",
                                min_chars=settings.agentaus_review_min_chars)
        ):
            reviewed = await helper_calls._self_review(client, _last_user_text(body), msg0["content"])
            if reviewed != msg0["content"]:
                msg0 = {**msg0, "content": reviewed}
                data = {**data, "choices": [{**choice0, "message": msg0}]
                        + list((data.get("choices") or [])[1:])}

        if (
            settings.agentaus_grounding_check
            and _checks_apply(body)
            and not msg0.get("tool_calls")
            and (ran or not worth_reviewing_turn(body))
            and _grounding_applies(body, msg0.get("content") or "", ran)
        ):
            grounded = await _check_grounding(client, body, msg0["content"], ran)
            if grounded != msg0.get("content"):
                msg0 = {**msg0, "content": grounded}
                data = {**data, "choices": [{**choice0, "message": msg0}]
                        + list((data.get("choices") or [])[1:])}

        message = agentaus_response_to_anthropic(
            data,
            model=display_model,
            thinking=plan_holder["text"] if settings.agentaus_thinking_visible else None,
        )
        rlog(logging.INFO,
            "POST /v1/messages model=%s route=agentaus stream=false -> 200 in %.1fs in=%s out=%s",
            display_model,
            time.monotonic() - started,
            message["usage"]["input_tokens"],
            message["usage"]["output_tokens"],
        )
        return JSONResponse(message)

    if not wants_stream:
        # A buffered client: Claude Code's fallback after a cut stream, or its re-send of one.
        # It joins the turn already running for the same request - the stream it replaces, or
        # an earlier buffered copy - and waits BRIDGE_NONSTREAM_DEADLINE at most. See flight.py.
        return await _answer_buffered(request, flight.key_of(received, request.headers),
                                      run_buffered, display_model)

    async def _refit(scale: float) -> dict:
        """Recompact against a tighter target and rebuild the upstream payload."""
        nonlocal body
        body = await context_window._fit_to_window(
            request, body, limit=_context_limit(), reserve=reserved, scale=scale
        )
        return anthropic_request_to_agentaus(
            body,
            system_prompt_overwrite=settings.system_prompt_overwrite,
            stream=settings.upstream_stream,
        )

    # Compaction is deliberately deferred into the generator rather than done here.
    # On a long conversation it takes a minute or more, and until this function
    # returns, Claude Code has received nothing at all - no headers, no message_start,
    # and critically no keepalive pings, because the response has not begun. That
    # looks exactly like a hung request: a short "hello" on a large session produced
    # no visible response for over 90 seconds. Starting the stream first lets pings
    # flow while the summarising happens.
    async def stream_with_compaction() -> AsyncIterator[bytes]:
        try:
            # message_start goes out at once, whatever happens next: what the client sees until
            # the answer is that, the plan or notes as they are written, and a heartbeat while
            # nothing else comes (see _waiting).
            builder = AnthropicStreamBuilder(
                display_model, input_tokens=estimate_request_tokens(body),
                chunk_chars=settings.chunk_chars,
            )
            yield builder.start()
            streamed_plan = False
            if settings.agentaus_live_draft and settings.agentaus_thinking_visible:
                # What preparation produces - the plan as it is written, a note when
                # compaction starts - streams into a live thinking block. Before this the
                # client saw nothing but pings until planning finished, so its token counter
                # sat at zero for 5-15 seconds on every agent turn.
                pieces: asyncio.Queue = asyncio.Queue()
                seen_plan = []

                def on_plan_piece(text: str) -> None:
                    seen_plan.append(text)
                    pieces.put_nowait(text)

                def on_note(text: str) -> None:
                    pieces.put_nowait(f"_{text}_\n\n")

                work = asyncio.ensure_future(prepare(body, 1.0, on_plan_piece, on_note))
                quiet_since = time.monotonic()
                try:
                    while not (work.done() and pieces.empty()):
                        try:
                            piece = await asyncio.wait_for(pieces.get(), timeout=0.25)
                        except asyncio.TimeoutError:
                            quiet = time.monotonic() - quiet_since
                            if 0 < settings.heartbeat_seconds <= quiet:
                                beat = _heartbeat(builder, time.monotonic() - started)
                                if beat:
                                    yield beat
                                quiet_since = time.monotonic()
                            continue
                        quiet_since = time.monotonic()
                        yield builder.live_thinking(piece)
                finally:
                    if not work.done():
                        work.cancel()
                prepared_body, prepared_payload = work.result()
                streamed_plan = bool(seen_plan)
                if plan_holder["text"] and not streamed_plan:
                    # Planned, but the stream fell back to a buffered call: show it whole.
                    yield builder.live_thinking(plan_holder["text"])
                    streamed_plan = True
            else:
                box: dict = {}
                async for beat in _waiting(prepare(body), builder, box):
                    yield beat
                prepared_body, prepared_payload = box["value"]

            async def refit(scale: float) -> dict:
                _, rebuilt = await prepare(prepared_body, scale)
                return rebuilt

            rlog(logging.INFO, "upstream start (est %d tok)",
                 estimate_request_tokens(prepared_body))
            async for chunk in agentaus_stream._agentaus_event_stream(
                client, prepared_payload, prepared_body, display_model, started, refit,
                plan=None if streamed_plan else plan_holder["text"], builder=builder,
                library=library_holder["names"], plain_text=plain_text,
                client_body=received, stop_retrying=stop_retrying,
                time_up_text=time_up_text, request_kind=request_kind,
            ):
                yield chunk
        except asyncio.CancelledError:
            # No client is left, or the turn ran out its deadline - flight.py logs which.
            rlog(logging.WARNING, "turn stopped after %.1fs", time.monotonic() - started)
            raise
        except GeneratorExit:
            rlog(logging.WARNING, "stream closed early after %.1fs",
                 time.monotonic() - started)
            raise

    # The turn runs as a flight: a client that leaves does not stop it at once, and an
    # identical request - Claude Code's re-send, or its buffered fallback - joins it instead of
    # starting another. The flight holds the BRIDGE_TURN_TIMEOUT deadline. See flight.py.
    key = flight.key_of(received, request.headers)
    running = flight.find(key, flight.STREAM)
    if running is not None:
        rlog(logging.INFO, "streamed request joins the turn already running for it (%.0fs in)",
             time.monotonic() - running.started)
    else:
        running = flight.start_stream(key, stream_with_compaction(),
                                      deadline=settings.turn_timeout_seconds)
    generator = _keepalive(
        _relay(running),
        settings.ping_interval_seconds,
        AnthropicStreamBuilder.ping,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

"""Spotting a turn that stopped before its task was done, and asking it to carry on."""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from .augment import announces_a_step, awaiting_background, could_be_a_refusal, working_directory_of
from .conversation_text import _last_user_text, _original_request
from .translate import has_unseen


# STALLED re-asks per conversation, across requests. The correction budget is per
# request, and Claude Code sends a fresh request after every tool call - so a judge that
# kept calling a finished turn stalled kept it going until the client's turn cap.
# Observed: 76 re-asks in one benchmark run, and a session that had finished its task
# pushed into writing notes into the user's Claude memory folder.
_STALL_REASKS: "OrderedDict[str, tuple[int, int, str, int]]" = OrderedDict()
_STALL_REASK_LIMIT = 2
# The budget refills once the model has made this many tool calls since its last re-ask.
# A lifetime budget of two was spent in the first minutes of a long ultracode session,
# and its last reply - "We will apply multiple Edit calls." - ended the run with the bug
# fixes unwritten. Calls made since the re-ask are progress; a model that keeps stopping
# without making any still runs out after two. Measured (influenca, devine): a model that
# announced every step ("Let me look at what's inside the uv tools dir.") took each one
# when re-asked - one call per re-ask - and a refill at three calls forwarded its third
# announcement as the answer. One call is progress.
_STALL_PROGRESS_CALLS = 1
# However much progress, at most this many re-asks per message the user types: the bound
# on a judge that keeps calling a finished turn stalled. Something new the user says starts
# a fresh budget - the summary-keyed budget alone was spent for the whole session.
_STALL_REASKS_PER_MESSAGE = 8
# A reply that plainly announces its next step is no judgement call, so it may be sent back
# this often per message - still only after a tool call each time, so a model that never
# acts runs out after two.
_STALL_ANNOUNCED_PER_MESSAGE = 40


def _stall_key(body: dict) -> str:
    return hashlib.sha256((_original_request(body) + "\x00"
                           + (working_directory_of(body) or "")).encode()).hexdigest()


def _tool_calls_made(body: dict) -> int:
    return sum(1 for m in body.get("messages") or [] if m.get("role") == "assistant"
               and isinstance(m.get("content"), list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use")


def _worth_checking(original: dict, answer: str, *, tools_offered: bool,
                    called_a_tool: bool) -> bool:
    """Whether a reply may be a refusal or a stall at all - the one gate in front of both
    the structural stall check and the judge, so the image exemption covers both."""
    # Started in the background and said so: the right reply, not a stall.
    return not awaiting_background(original) and could_be_a_refusal(
        answer, tools_offered=tools_offered, called_a_tool=called_a_tool,
        answers_an_image=has_unseen((original.get("messages") or [])[-1:]))


def _structural_stall(answer: str, missing: list) -> tuple[bool, list, bool]:
    """(stalled on its face, files still to ask for, announced) for a reply with no tool
    call - decided without a judge call.

    - A reply ending in a question is a fair place to stop, files or not, and
      `announces_a_step` never reads one as an announcement either: it goes to the judge.
    - An announcement is a step not taken, whatever is missing: asking it for the files at
      that point pushed a model to write them before it had read anything.
    - Only an announcement is `announced`, which earns the larger re-ask budget. Missing
      files are the less certain signal (the request may not mean them), so they share
      the judge's.
    """
    if (answer or "").rstrip().endswith("?"):
        missing = []
    announced = announces_a_step(answer)
    if announced:
        missing = []
    return announced or bool(missing), missing, announced


def _may_reask_stalled(body: dict, *, announced: bool = False) -> bool:
    """Whether a reply judged stalled may be sent back, charging the budget if so.

    Two counters per conversation, both reset by a new message from the user:

    - `used`: re-asks since the model last made progress. Any tool call since the last
      re-ask resets it, and at `_STALL_REASK_LIMIT` (2) no more are sent. This is what bounds
      a misfire: a finished answer wrongly re-asked gets no tool call back, so it costs two
      re-asks at most, whatever the per-message cap.
    - `total`: re-asks for this user message, capped at `_STALL_ANNOUNCED_PER_MESSAGE`
      when `announced` (a reply that plainly says which tool step it takes next) and at
      `_STALL_REASKS_PER_MESSAGE` otherwise (judged by the model, or ended without the
      files the request names - both less certain than an announcement). Only a model
      that keeps making progress between re-asks reaches these caps - a long task can
      announce every one of its steps.
    """
    key = _stall_key(body)
    calls = _tool_calls_made(body)
    said = _last_user_text(body)
    used, at, before, total = _STALL_REASKS.get(key, (0, calls, said, 0))
    if said != before:
        used, total = 0, 0
    elif calls - at >= _STALL_PROGRESS_CALLS:
        used = 0
    cap = _STALL_ANNOUNCED_PER_MESSAGE if announced else _STALL_REASKS_PER_MESSAGE
    if used >= _STALL_REASK_LIMIT or total >= cap:
        return False
    _STALL_REASKS[key] = (used + 1, calls, said, total + 1)
    _STALL_REASKS.move_to_end(key)
    while len(_STALL_REASKS) > 512:
        _STALL_REASKS.popitem(last=False)
    return True

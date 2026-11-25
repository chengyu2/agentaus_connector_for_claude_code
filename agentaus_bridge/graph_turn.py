"""Turns that belong to the agent graph: launching it, waiting on it,
and keeping its script out of later requests."""

from __future__ import annotations

import json
import logging

from . import graph

from .augment import (
    _tools_used, awaiting_background, is_subagent_turn, workflow_offered, workflow_opted_in,
    working_directory_of,
)
from .config import settings
from .conversation_text import _last_user_text, _original_request, _user_texts, _without_reminders
from .request_log import rlog


def _graph_main_loop(body: dict) -> bool:
    """A main loop whose `Workflow` call the bridge runs its graph in place of. Opted in is not
    enough: with `Workflow` still deferred there is no call to run the graph for, and treating
    the session as one skipped the planning pass and held every write for a Workflow call
    the model could not make. Nor once the main loop has delegated with `Agent` (as
    `focus_tools` keeps `Agent` then): loading `Workflow` afterwards must not refuse the write
    that merges the subagents' results, or relaunch the whole request as a graph."""
    return bool(settings.agentaus_workflow_graph and not is_subagent_turn(body)
                and workflow_opted_in(body) and workflow_offered(body)
                and "Agent" not in _tools_used(body))


def _writes_plain_text(body: dict) -> bool:
    """Whether this request's text writes are made plain - see graph.plain_text_writes. A
    graph agent's, and an opted-in main loop's; a plain user session is left as it is."""
    return (graph.is_graph_task(body) and is_subagent_turn(body)) or _graph_main_loop(body)


def _graph_session(body: dict) -> dict | None:
    """This session's graph record (see graph.note_run), from the body as the client sent it."""
    entry = graph.note_run(body)
    if entry is not None and entry.get("said") is None:
        # Started from a launch the bridge had no record of: what the user had said by then.
        cut = entry.get("found_at") or 0
        entry["said"] = _last_user_text({"messages": (body.get("messages") or [])[:cut]})
    return entry


GRAPH_ALREADY_RAN = (
    "Your `Workflow` call was not run: the agent graph already ran in this session and "
    "reported. Make the fix its result names yourself: `Read` the lines, copy `old_string` "
    "from that Read exactly (indentation included), and `Edit`."
)
GRAPH_STILL_RUNNING = (
    "Your `Workflow` call was not run: the agent graph is already running in this session "
    "({tasks}). Do not start another. Wait for its result - it arrives in a message "
    "containing `<task-notification>` - or end your turn saying what is running."
)


def _graph_reruns(calls: list, body: dict) -> list:
    """(call, correction) for a second `Workflow` call in a session whose graph has run.

    Measured (d12 he10, c1913h): a second graph, started after the first had reported, ran on
    a 0-char task until the 7,200 s kill, and its picks would have taken he10 from 0.8 to 0.6.
    A new request from the user since the graph started may start a new one; while one is
    running, nothing may. Pass the body as the client sent it.
    """
    if not _graph_main_loop(body):
        return []
    workflows = [c for c in calls if c.get("name") == "Workflow"]
    if not workflows:
        return []
    entry = _graph_session(body)
    state = graph.run_state(entry)
    if state == "running":
        tasks = ", ".join(sorted(t for t in entry["launched"] - entry["reported"] if t != "?"))
        return [(c, GRAPH_STILL_RUNNING.format(tasks=tasks or "launched earlier")) for c in workflows]
    if state == "reported" and not _new_request_since(body, entry):
        return [(c, GRAPH_ALREADY_RAN) for c in workflows]
    return []


def _new_request_since(body: dict, entry: dict) -> bool:
    """Whether the user has asked for something since the graph started. Nothing said (the
    request compacted away) and Claude Code's own compaction summary are not a new request."""
    said = _last_user_text(body)
    return bool(said) and said != (entry.get("said") or "") and not said.startswith(
        "This session is being continued from a previous conversation")


def _graph_not_started(body: dict) -> bool:
    """A graph main loop before its first Workflow call - the body as the client sent it."""
    if not _graph_main_loop(body) or graph.run_state(graph.note_run(body)) != "none":
        return False
    return not any(
        isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Workflow"
        for m in body.get("messages") or [] if m.get("role") == "assistant"
        and isinstance(m.get("content"), list) for b in m["content"])


def _without_graph_script(body: dict) -> tuple[dict, int]:
    """`body` with each Workflow call that carries the bridge's graph cut to its description
    and a stand-in script. Measured (d12 c1913h): one such call held 115,148 characters of
    script, skills and inventory, and on the first request after the graph reported it alone
    pushed the conversation into compaction - which summarised the call away."""
    swapped = 0
    messages = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            messages.append(message)
            continue
        blocks = []
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") == "Workflow" and isinstance(block.get("input"), dict)
                    and graph.is_graph_script(block["input"].get("script"))):
                swapped += 1
                block = {**block, "input": {"description": block["input"].get("description") or "",
                                            "script": graph.SCRIPT_STAND_IN}}
            blocks.append(block)
        messages.append({**message, "content": blocks})
    return ({**body, "messages": messages}, swapped) if swapped else (body, 0)


def _run_graph_instead(calls: list, body: dict) -> None:
    """Put the bridge's agent graph in place of a script Agentaus wrote. See graph.py.

    Pass the body as the client sent it: the task is the user's request, and compaction can
    summarise that away."""
    if not _graph_main_loop(body):
        return
    # The graph sees the whole request: on a follow-up turn, the opening request and the
    # latest message together, not only "now do the next file".
    first, last = _original_request(body), _last_user_text(body)
    task = first if not last or last == first else f"{first}\n\nFollow-up:\n{last}"
    if not task.strip():
        # Measured (d12 he10, c1913h): "running the agent graph for a 0-char request" - its
        # planners invented the rules the solvers then followed.
        task = (graph.note_run(body) or {}).get("task") or ""
        if not task.strip():
            if any(c.get("name") == "Workflow" for c in calls):
                rlog(logging.WARNING, "workflow: no request to run the agent graph on; "
                     "leaving the call alone")
            return
    if not graph.suits(task, working_directory_of(body)):
        return
    said = "\n".join(_user_texts(body))
    for call in calls:
        if call.get("name") != "Workflow":
            continue
        script = graph.script_of(call)
        # A script the user wrote or dictated is theirs to run, not ours to replace.
        if script and len(script) >= 200 and script.strip()[:200] in said:
            rlog(logging.INFO, "workflow: running the user's own script")
            continue
        before = call.get("arguments")
        call["arguments"] = graph.expand(call, task, working_directory_of(body))["arguments"]
        if call["arguments"] != before:
            graph.remember_run(body, task, _last_user_text(body))
            rlog(logging.INFO, "workflow: substituted the agent graph")


GRAPH_WAITING = ("The workflow is running in the background; I will check its result when it "
                 "finishes.")


def _graph_launch(body: dict) -> dict | None:
    """The Workflow call that starts the agent graph, when an opted-in main loop has not
    started it and the newest message is the user's; None otherwise.

    Measured (d16 tender a5): the model read three files and wrote its answer in the chat -
    no Workflow call, no response.md, a 0.0. Whether the graph runs is the user's choice
    ("ultracode"), not the model's, so the bridge makes the call itself.
    """
    if not _graph_main_loop(body):
        return None
    if "Workflow" not in {(t or {}).get("name") for t in body.get("tools") or []}:
        return None
    messages = [m for m in body.get("messages") or [] if m.get("role") != "system"]
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Workflow"
                for b in content):
            return None
    if not messages or messages[-1].get("role") != "user":
        return None
    last = messages[-1].get("content")
    typed = _without_reminders(last) if isinstance(last, str) else _without_reminders("\n".join(
        b.get("text", "") for b in last or [] if isinstance(b, dict) and b.get("type") == "text"))
    if not typed or "<task-notification>" in typed:
        return None
    first, latest = _original_request(body), _last_user_text(body)
    task = first if not latest or latest == first else f"{first}\n\nFollow-up:\n{latest}"
    cwd = working_directory_of(body)
    if not task.strip() or not graph.suits(task, cwd):
        return None
    call = graph.expand({"name": "Workflow", "arguments": {"description": "Agentaus agent graph"}},
                        task, cwd)
    arguments = call.get("arguments")
    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(arguments, dict) or not arguments.get("script"):
        return None
    graph.remember_run(body, task, latest)
    return arguments


def _graph_waiting_reply(body: dict) -> str | None:
    """The reply to a graph main loop's "Workflow launched in background" turn, or None.

    Measured (d12 c1913): told to end its turn, the model ran `ls -R ~/.claude/projects`
    (prompted by the launch result's "Transcript dir" line), got 705 KB, and the conversation
    never fitted the window again - the graph's result was never read. Only when the newest
    user message is that launch's result and holds nothing the user typed.
    """
    if not _graph_main_loop(body) or not awaiting_background(body):
        return None
    messages = [m for m in body.get("messages") or [] if m.get("role") != "system"]
    if len(messages) < 2 or messages[-1].get("role") != "user":
        return None
    content, previous = messages[-1].get("content"), messages[-2].get("content")
    uses = [b for b in previous or [] if isinstance(b, dict) and b.get("type") == "tool_use"] \
        if isinstance(previous, list) else []
    if not uses or any(u.get("name") != "Workflow" for u in uses) or not isinstance(content, list):
        return None
    if any(isinstance(b, dict) and b.get("type") == "text" and _without_reminders(b.get("text", ""))
           for b in content):
        return None
    return GRAPH_WAITING

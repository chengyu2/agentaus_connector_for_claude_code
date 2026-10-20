"""The agent graph the bridge runs when Agentaus calls `Workflow`.

Measured over eight ultracode rounds, the weakest thing Agentaus did was write the
workflow: scripts on one line, unbalanced brackets, `Bash()` called inside a script,
subagent prompts like "compute the required statistics" with no rules in them, a
verifier told to "repeat the calculations as before", and no result ever passed from one
agent to the next. Every fix aimed at the script it wrote was a rule it then had to follow.

So the bridge supplies the graph instead (`graphs/solve.js`), and Agentaus only decides to
run it. It is built on four principles, each from how this model behaves: many agents;
one small, simple prompt per agent; many steps; and redundancy - a backup for every step
that can fail, and every answer reached three independent ways before it is trusted.

The substitution happens on the model's tool call, before Claude Code sees it: the
script is replaced, and `args` carries the user's request and the working directory. A
call naming a saved workflow, resuming a run, or re-running a script file is the user's
or Claude Code's own and passes through untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .config import settings

log = logging.getLogger("agentaus-bridge")

MARK = "[agentaus-graph]"
_GRAPHS = Path(__file__).parent / "graphs"
SCRIPT = (_GRAPHS / "solve.js").read_text()
DYNAMIC_SCRIPT = (_GRAPHS / "dynamic.js").read_text()
# Every role in the dynamic graph works from a skill file: scout, plan, review-plan, solve,
# merge, check, assemble, fix. Passed in with the script, so no agent spends a call reading
# its own instructions off disk.
SKILLS = {p.stem: p.read_text() for p in sorted((_GRAPHS / "skills").glob("*.md"))}


def script_for() -> str:
    return DYNAMIC_SCRIPT if settings.agentaus_graph_kind == "dynamic" else SCRIPT


def expand(call: dict, task: str, cwd: str | None) -> dict:
    """`call` with the graph in place of the script Agentaus wrote, or unchanged."""
    if call.get("name") != "Workflow":
        return call
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    if any(arguments.get(k) for k in ("name", "resumeFromRunId", "scriptPath")):
        return call
    dynamic = settings.agentaus_graph_kind == "dynamic"
    expanded = {
        "description": arguments.get("description") or "Agentaus agent graph",
        "script": script_for(),
        "args": {"task": task, "cwd": cwd or "", "width": settings.agentaus_graph_width,
                 **({"skills": SKILLS} if dynamic else {})},
    }
    log.info("workflow: running the agent graph for a %d-char request", len(task or ""))
    return {**call, "arguments": json.dumps(expanded)}


def script_of(call: dict) -> str:
    """The inline script a Workflow call carries, or ""."""
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return ""
    return (arguments or {}).get("script") or "" if isinstance(arguments, dict) else ""


def is_graph_task(body: dict) -> bool:
    """Whether a subagent request was started by the graph - its prompt carries MARK."""
    for message in body.get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or [] if isinstance(b, dict)]
        if any(MARK in (t or "") for t in texts):
            return True
    return False


# A graph subagent has one small job. These are all it needs; the rest - starting agents
# or workflows of its own, skills, the web, MCP connectors - is room to wander off.
GRAPH_TOOLS = {"Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS",
               "StructuredOutput"}


THINK = "[think-only]"


def _is_think_only(body: dict) -> bool:
    for message in body.get("messages") or []:
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or [] if isinstance(b, dict)]
        if any(THINK in (t or "") and MARK in (t or "") for t in texts):
            return True
    return False


def focus(body: dict) -> tuple[dict, list]:
    """A graph subagent's request with only the tools its job uses.

    A think-only agent - one the graph marks as reasoning over text it was handed - gets
    `StructuredOutput` alone, so it answers in one call instead of a tool loop.
    """
    tools = body.get("tools") or []
    think = _is_think_only(body)
    allowed = {"StructuredOutput"} if think else GRAPH_TOOLS
    kept = [t for t in tools if (t or {}).get("name") in allowed]
    # A think-only agent with no schema to fill gets no tools at all: it writes Markdown.
    if think and not kept:
        return ({**body, "tools": []}, [(t or {}).get("name") or "" for t in tools]) if tools else (body, [])
    if not kept or len(kept) == len(tools):
        return body, []
    dropped = [(t or {}).get("name") or "" for t in tools if t not in kept]
    return {**body, "tools": kept}, dropped


# The most of one tool result a graph subagent's conversation keeps. Measured: graph
# subagents printed whole spreadsheets and PDFs into their own context, 108 requests ran
# past the 131k window, and each backup agent did the same thing again.
RESULT_CHARS = 16_000


def trim_results(body: dict) -> tuple[dict, int]:
    """A graph subagent's request with each oversized tool result cut to head and tail."""
    cut = 0
    messages = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            messages.append(message)
            continue
        blocks = []
        for block in content:
            text = block.get("content") if isinstance(block, dict) and block.get("type") == "tool_result" else None
            if isinstance(text, list):
                text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
            if isinstance(text, str) and len(text) > RESULT_CHARS:
                cut += 1
                head, tail = text[: RESULT_CHARS * 2 // 3], text[-RESULT_CHARS // 4:]
                block = {**block, "content": (
                    f"{head}\n\n[... {len(text) - len(head) - len(tail):,} characters cut. Print "
                    f"less: counts, head, a few rows, or the lines you need ...]\n\n{tail}")}
            blocks.append(block)
        messages.append({**message, "content": blocks})
    return ({**body, "messages": messages}, cut) if cut else (body, 0)


_CODE_PATH = re.compile(r"\b[\w./-]+\.(py|js|mjs|jsx|ts|tsx|r|sh|go|java|kt|rb|rs|c|h|cpp|cs|swift|php)\b", re.I)
_CODE_WORK = re.compile(r"\b(implement|bugs?|fix|fixes|refactor|function|functions|feature|tests?)\b", re.I)


def _has_code(cwd: str | None) -> bool:
    if not cwd or not os.path.isdir(cwd):
        return False
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        if root[len(cwd):].count(os.sep) > 1:
            dirs[:] = []
        if any(_CODE_PATH.search(f) for f in files):
            return True
    return False


def suits(task: str, cwd: str | None) -> bool:
    """Whether a task is one the graph does better than Agentaus's own workflow.

    Measured: the graph took a spreadsheet analysis from 0.61 to 1.00 and a tender response
    from 0.71 to 0.80, but on code it lost - ten functions in one file scored 0.30-0.50
    through the graph and 0.80 through Agentaus's own script. So code work stays with the
    model's own workflow unless AGENTAUS_GRAPH_FOR_CODE says otherwise.
    """
    # The dynamic graph plans the shape of each task, and code is where it gains most.
    if settings.agentaus_graph_kind == "dynamic" or settings.agentaus_graph_for_code:
        return True
    text = task or ""
    if _CODE_PATH.search(text):
        return False
    return not (_CODE_WORK.search(text) and _has_code(cwd))

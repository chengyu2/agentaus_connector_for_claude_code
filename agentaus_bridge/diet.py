"""A shorter, sharper prompt for Agentaus (AGENTAUS_PROMPT_DIET).

A trivial Claude Code request reaches Agentaus at ~13k tokens: Claude Code's system prompt
and tool schemas, plus what the bridge adds (operating notes, a tool-selection section,
skill and subagent routing, a skill library listing, five bridge tools). Much of it is
written for Claude - long tool manuals, memory-file conventions, Claude model ids - or is
advice this model rarely acts on. A weaker model follows a short prompt better, so with the
diet on:

- `LEAN_GUIDANCE` replaces the long operating notes and the tool-selection section
  (see augment.guidance_for);
- the skill library, routing section and the rarely used bridge tools default off
  (see config._DIET);
- `slim()` caps every tool description and parameter description, and drops the Claude
  Code system-prompt sections that do not apply to this model.

Nothing here changes what a tool does or accepts: schemas keep every property, only the
prose about them is shortened.
"""

from __future__ import annotations

import re

LEAN_GUIDANCE = """\

--- Operating notes ---

You are working in a real environment through tools. Each reply makes at most one tool
call, so make it the one that moves the task forward.

1. Act, do not describe. If something needs doing - reading, editing, running - call the
tool now. Never stop at a plan, and never tell the user what you would do instead.
2. Look before you change. Read a file before editing it; do not guess at paths, APIs or
flags. Do not repeat a call whose result you already have.
3. Handle the whole requirement: empty input, boundaries (< vs <=), duplicates, invalid
input that should raise - not just the obvious path.
4. Verify by running. After changing code, run it - the tests, the script, or a call
that exercises what you changed (compiling is not enough) - and fix what fails. Code you
have not run is not done. Re-read what you changed once: a renamed name or changed value
must be updated at every use, inside the function body as well as at call sites.
5. Finish with the deliverable written: the file edited or created, the requested output
on disk. Then reply in a few lines (Markdown) saying what you did and the result,
including anything you could not verify.
"""

# Claude Code system-prompt sections that are about Claude or about Claude Code's own
# housekeeping, not about the task: dropped from Agentaus turns.
_DROP_SECTIONS = ("Memory", "Context management")
# Single lines of the same kind.
_DROP_LINES = re.compile(
    r"^\s*-\s*(The most recent Claude models are|Claude Code is available as a CLI|"
    r"Fast mode for Claude Code)\b.*$\n?",
    re.M,
)

DESCRIPTION_CAP = 900
PARAMETER_CAP = 200


def _drop_sections(text: str) -> str:
    for name in _DROP_SECTIONS:
        text = re.sub(rf"(?ms)^# {re.escape(name)}\s*$.*?(?=^# |\Z)", "", text)
    return _DROP_LINES.sub("", text)


def _cap(text: str, limit: int) -> str:
    """`text` cut to `limit` chars at a section heading or line boundary."""
    if len(text) <= limit:
        return text
    heading = re.search(r"(?m)^#{1,3} ", text)
    if heading and 0 < heading.start() <= limit:
        return text[: heading.start()].rstrip()
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind(". ", 0, limit) + 1
    return text[: cut if cut > 0 else limit].rstrip()


def _first_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    end = text.find(". ", 0, limit)
    return text[: end + 1] if end > 0 else text[:limit].rstrip() + "..."


def _slim_schema(schema):
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key == "description" and isinstance(value, str):
                out[key] = _first_sentence(value, PARAMETER_CAP)
            else:
                out[key] = _slim_schema(value)
        return out
    if isinstance(schema, list):
        return [_slim_schema(v) for v in schema]
    return schema


def _slim_tool(tool: dict) -> dict:
    if not isinstance(tool, dict):
        return tool
    out = dict(tool)
    if isinstance(out.get("description"), str):
        out["description"] = _cap(out["description"], DESCRIPTION_CAP)
    if isinstance(out.get("input_schema"), dict):
        out["input_schema"] = _slim_schema(out["input_schema"])
    return out


def _slim_system(system):
    if isinstance(system, str):
        return _drop_sections(system)
    if isinstance(system, list):
        return [{**b, "text": _drop_sections(b["text"])}
                if isinstance(b, dict) and isinstance(b.get("text"), str) else b
                for b in system]
    return system


def slim(body: dict) -> dict:
    """`body` with shorter tool prose and without the sections that do not apply."""
    out = dict(body)
    if "system" in out:
        out["system"] = _slim_system(out["system"])
    if isinstance(out.get("tools"), list):
        out["tools"] = [_slim_tool(t) for t in out["tools"]]
    return out

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
- `slim()` caps tool descriptions (except the core tools', see `UNCAPPED_TOOLS`) and
  parameter descriptions, and drops the Claude Code system-prompt sections that do not
  apply to this model.

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

# Tools whose descriptions are never capped. Their manuals carry the rules that keep the
# model safe and correct, often past the first heading: Bash's "# Instructions" and its
# git section (only commit when asked, never force push, never change git config), Edit's
# read-first and unique-match rules, Agent/Skill's when-to-use. No length rule can tell a
# safety paragraph from filler, so these are left whole; the savings are in the long tail
# of other tools (MCP servers, web, todo, plan mode). Parameter prose is still shortened.
UNCAPPED_TOOLS = frozenset({
    "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Task", "Skill", "Read",
})

# Where a dropped section ends: the next `#`/`##` heading, or the environment block when
# it has no heading of its own (older Claude Code: "Here is useful information about the
# environment..." followed by <env>...</env>). Stopping early at a subheading can leave a
# stray subsection behind; running on would delete the environment.
_SECTION_END = r"(?=^#{1,2} |^Here is useful information|^\s*<env>|\Z)"


def _drop_sections(text: str) -> str:
    for name in _DROP_SECTIONS:
        text = re.sub(rf"(?ms)^# {re.escape(name)}\s*$.*?{_SECTION_END}", "", text)
    return _DROP_LINES.sub("", text)


def _headings(text: str):
    """Offsets of Markdown headings (`#` to `###`) outside ``` code fences."""
    pos, fenced = 0, False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,3} ", line):
            yield pos
        pos += len(line)


def _cap(text: str, limit: int) -> str:
    """`text` cut to `limit` chars at a section heading or line boundary.

    A heading is used only in the second half of the budget: cutting at one far short of
    the cap threw away most of what the cap allows.
    """
    if len(text) <= limit:
        return text
    near = [h for h in _headings(text) if limit // 2 <= h <= limit]
    if near:
        return text[: near[-1]].rstrip()
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind(". ", 0, limit) + 1
    return text[: cut if cut > 0 else limit].rstrip()


# A sentence end: `.`/`!`/`?` followed by whitespace and a capital letter, not after a
# common abbreviation ("e.g. ", "i.e. ", "etc. ", "vs. ").
_SENTENCE_END = re.compile(
    r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)[.!?](?=\s+[A-Z])"
)


def _first_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    end = _SENTENCE_END.search(text, 0, limit)
    return text[: end.end()] if end else text[:limit].rstrip() + "..."


# Schema keys whose values are data, not schema: a "description" inside them is a value
# (a default object, an example, an enum member) and must not be rewritten.
_DATA_KEYS = frozenset({"default", "examples", "example", "enum", "const"})


def _slim_schema(schema):
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key == "description" and isinstance(value, str):
                out[key] = _first_sentence(value, PARAMETER_CAP)
            elif key in _DATA_KEYS:
                out[key] = value
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
    if isinstance(out.get("description"), str) and out.get("name") not in UNCAPPED_TOOLS:
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

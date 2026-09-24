"""Loading the project's own skills, because this model will not load them itself.

A skill is Markdown that tells the model *how* to do a kind of task. Claude Code offers
it through a `Skill` tool the model is expected to call when a description matches what
was asked.

Measured, on the same prompt in the same directory with the same skills present and
`Skill` offered among 28 tools:

    opus      ['Bash', 'Bash', 'Skill', 'Bash', ...]   invoked Skill: True
    agentaus  ['Read']                                 invoked Skill: False   (x4)

Naming `Skill` first in the tool-selection guidance did not change it: still `['Read']`.
So on Agentaus the repository's skills - the bridge's main compensation strategy - were
not reaching the model they were written for.

The fix is the same one the rest of the bridge already applies to this model: do not ask
it to choose, hand it the thing. A skill whose gate fires is injected into the system
prompt directly, and the `Skill` tool remains for the ones no gate covers.

Nothing here is Agentaus-specific except when it runs. The files are ordinary Claude Code
skills and are read, never rewritten.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("agentaus-bridge")

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_NAME = re.compile(r"^name:\s*(.+?)\s*$", re.M)

# One skill is worth a couple of thousand tokens of a 131k window. Two is the most any
# turn gets, so a directory of twenty cannot quietly consume the conversation.
_MAX_SKILL_CHARS = 9000
_MAX_SKILLS = 2


def _read_skill(path: str) -> tuple[str, str] | None:
    """(name, body) for one SKILL.md, or None if it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(_MAX_SKILL_CHARS * 2)
    except OSError:
        return None
    front = _FRONT_MATTER.match(text)
    body = text[front.end():] if front else text
    named = _NAME.search(front.group(1)) if front else None
    name = named.group(1) if named else os.path.basename(os.path.dirname(path))
    body = body.strip()
    if not body:
        return None
    if len(body) > _MAX_SKILL_CHARS:
        body = body[:_MAX_SKILL_CHARS] + "\n[...truncated]"
    return name, body


# Parsed skills, keyed by the skills directory and its mtime. Bounded, because the
# bridge is long-lived and an unbounded cache is the thing a previous commit had to
# remove from document conversion.
#
# This is disk work on the request path, and the commit that moved search into worker
# threads established that such work must not block the event loop. Measured here at
# 0.22ms for ten skills - well under that fix's 0.1s probe - but it grows with the
# number of skills and with a slower filesystem, and a read that repeats on every turn
# to return the same bytes is worth doing once.
_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_CACHE_MAX = 16


def available(cwd: str | None) -> dict[str, str]:
    """Every skill in the project, by name. Empty when there is no skills directory.

    Re-read only when the skills directory's mtime changes, which covers a skill being
    added, removed or renamed. An edit to a SKILL.md body does not move the directory's
    mtime, so a running bridge keeps the version it loaded - restart it, as you would
    for any other change to what the bridge sends.
    """
    if not cwd:
        return {}
    root = os.path.join(os.path.expanduser(cwd), ".claude", "skills")
    try:
        stamp = os.stat(root).st_mtime
    except OSError:
        return {}

    cached = _CACHE.get(root)
    if cached and cached[0] == stamp:
        return cached[1]

    found: dict[str, str] = {}
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return {}
    for entry in entries:
        skill = _read_skill(os.path.join(root, entry, "SKILL.md"))
        if skill:
            found[skill[0]] = skill[1]

    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[root] = (stamp, found)
    return found


def render(cwd: str | None, wanted: list[str]) -> str:
    """The named skills, as a block for the system prompt.

    Silent when a named skill is not present: a bridge that announces a procedure the
    project does not have teaches the model a method it cannot follow, which is the same
    mistake as describing a tool that is not on the wire.
    """
    if not wanted:
        return ""
    have = available(cwd)
    chosen = [(n, have[n]) for n in wanted if n in have][:_MAX_SKILLS]
    if not chosen:
        return ""
    log.info("injecting skill(s): %s", ", ".join(n for n, _ in chosen))
    blocks = [
        f"<skill name=\"{name}\">\n{body}\n</skill>" for name, body in chosen
    ]
    return (
        "\n\n<applicable_procedures>\n"
        "This project documents how work of this kind is done here. Follow it for this "
        "turn; it is more specific than your general habits and it was written because "
        "the obvious approach failed.\n\n"
        + "\n\n".join(blocks)
        + "\n</applicable_procedures>\n"
    )


# An absolute path, as it appears anywhere in a request.
_ABS_PATH = re.compile(r"/(?:[\w.\-]+/){1,14}[\w.\-]+")

# Bounded so a long prompt full of paths cannot turn one turn into a stat storm.
_MAX_CANDIDATES = 60

# How far up from a named file to look for the project root.
_MAX_DEPTH = 6


def locate(system, body: dict) -> str | None:
    """The directory whose `.claude/skills` exists, found by testing rather than parsing.

    The bridge used to read the working directory out of a labelled line in Claude Code's
    system prompt. That label is gone in 2.1.278 - the prompt carries no "working
    directory:" anywhere - so the parse silently returned None and every skill lookup
    quietly found nothing. A label can be renamed; a directory either has a skills folder
    or it does not, so this asks the filesystem instead.

    Candidates come from the request itself, longest first: `/a/b/c` is checked before
    `/a`, because the project directory is more specific than whatever contains it.
    """
    text = system if isinstance(system, str) else "\n".join(
        b.get("text", "") for b in (system or []) if isinstance(b, dict) and b.get("text")
    )
    for message in (body.get("messages") or [])[:3]:
        content = message.get("content")
        if isinstance(content, str):
            text += "\n" + content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += "\n" + str(block.get("text") or "")

    seen: list[str] = []
    for path in _ABS_PATH.findall(text[:60000]):
        # A file was named; its directory is the candidate. Then walk up: a request
        # usually names a file inside the project rather than the project itself, and
        # `/a/b/c/data.csv` should still find the skills that live at `/a/b`.
        if "." in os.path.basename(path):
            path = os.path.dirname(path)
        for _ in range(_MAX_DEPTH):
            if not path or path == "/":
                break
            if path not in seen:
                seen.append(path)
            path = os.path.dirname(path)
    # Longest first: the project directory is more specific than whatever contains it,
    # and a skills folder nearer the work wins over one further away.
    seen.sort(key=len, reverse=True)

    for path in seen[:_MAX_CANDIDATES]:
        try:
            if os.path.isdir(os.path.join(path, ".claude", "skills")):
                return path
        except OSError:
            continue
    return None

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

from . import prompt_style

log = logging.getLogger("agentaus-bridge")

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_NAME = re.compile(r"^name:\s*(.+?)\s*$", re.M)
# A one-line description, or a folded/literal block (`>` or `|`) of indented lines.
_DESCRIPTION = re.compile(r"^description:[ \t]*(.*)\n((?:[ \t]+.*\n?)*)", re.M)

# One skill is worth a couple of thousand tokens of a 131k window. Two is the most any
# turn gets, so a directory of twenty cannot quietly consume the conversation.
_MAX_SKILL_CHARS = 9000
_MAX_SKILLS = 2


def _description(front: str) -> str:
    found = _DESCRIPTION.search(front + "\n")
    if not found:
        return ""
    head, rest = found.group(1).strip(), found.group(2)
    text = " ".join(line.strip() for line in rest.splitlines()) if head in (">", "|", ">-", "|-") \
        else head
    return " ".join(text.strip().strip("\"'").split())


def _read_skill(path: str, max_chars: int = _MAX_SKILL_CHARS) -> tuple[str, str, str] | None:
    """(name, description, body) for one SKILL.md, or None if it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_chars * 2)
    except OSError:
        return None
    front = _FRONT_MATTER.match(text)
    body = text[front.end():] if front else text
    named = _NAME.search(front.group(1)) if front else None
    name = named.group(1) if named else os.path.basename(os.path.dirname(path))
    body = body.strip()
    if not body:
        return None
    if len(body) > max_chars:
        body = body[:max_chars] + "\n[...truncated]"
    return name, _description(front.group(1)) if front else "", body


# Parsed skills, keyed by the skills directory and its mtime. Bounded, because the
# bridge is long-lived and an unbounded cache is the thing a previous commit had to
# remove from document conversion.
#
# This is disk work on the request path, and the commit that moved search into worker
# threads established that such work must not block the event loop. Measured here at
# 0.22ms for ten skills - well under that fix's 0.1s probe - but it grows with the
# number of skills and with a slower filesystem, and a read that repeats on every turn
# to return the same bytes is worth doing once.
_CACHE: dict[str, tuple[float, dict[str, tuple[str, str]]]] = {}
_CACHE_MAX = 16


def available(cwd: str | None) -> dict[str, str]:
    """Every skill in the project, name -> body. Empty when there is no skills directory."""
    return {name: body for name, (_, body) in _load(cwd).items()}


def described(cwd: str | None) -> dict[str, str]:
    """Every skill in the project, name -> the description in its front matter."""
    return {name: description for name, (description, _) in _load(cwd).items()}


def _load(cwd: str | None) -> dict[str, tuple[str, str]]:
    if not cwd:
        return {}
    return _load_root(os.path.join(os.path.expanduser(cwd), ".claude", "skills"))


def _load_root(root: str, max_chars: int = _MAX_SKILL_CHARS) -> dict[str, tuple[str, str]]:
    """Every skill in the project, name -> (description, body).

    Re-read only when the skills directory's mtime changes, which covers a skill being
    added, removed or renamed. An edit to a SKILL.md body does not move the directory's
    mtime, so a running bridge keeps the version it loaded - restart it, as you would
    for any other change to what the bridge sends.
    """
    try:
        stamp = os.stat(root).st_mtime
    except OSError:
        return {}

    cached = _CACHE.get(root)
    if cached and cached[0] == stamp:
        return cached[1]

    found: dict[str, tuple[str, str]] = {}
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return {}
    for entry in entries:
        skill = _read_skill(os.path.join(root, entry, "SKILL.md"), max_chars)
        if skill:
            found[skill[0]] = (skill[1], skill[2])

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
    lead = ("This project documents how work of this kind is done here. Follow it for this "
            "turn; it is more specific than your general habits and it was written because "
            "the obvious approach failed.")
    if prompt_style.markdown():
        # A skill is instructions written for the model, in Markdown already - so it goes
        # in under headings, not fenced like data that must not be read as structure.
        blocks = [f"### Skill: {name}\n\n{body}" for name, body in chosen]
        return "\n\n## Applicable procedures\n\n" + lead + "\n\n" + "\n\n".join(blocks) + "\n"
    blocks = [
        f"<skill name=\"{name}\">\n{body}\n</skill>" for name, body in chosen
    ]
    return (
        "\n\n<applicable_procedures>\n"
        + lead + "\n\n"
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


# --------------------------------------------------------------------------------------
# The listing Claude Code sends
# --------------------------------------------------------------------------------------
#
# Claude Code announces skills in its prompt as "The following skills are available for
# use with the Skill tool:" followed by one "- name: description" line each. Since
# 2.1.281 a PROJECT skill arrives as a bare "- name" - for every model, Opus included -
# while bundled and plugin skills keep their descriptions. Opus still picks the right
# one from the name; Agentaus does not. Measured on the same requests with the
# descriptions put back: analyse-data 8/8, read-documents 8/8, code-review 5/8, and no
# call at all on a trivial question - so the choice is there once it can be made.

_LISTING_HEAD = "The following skills are available for use with the Skill tool:"
_ENTRY = re.compile(r"^- ([^\s:]+)(?::\s*(.*))?$")


def describe_listing(text: str, descriptions: dict[str, str]) -> tuple[str, int]:
    """`text` with each bare skill entry given its description. Returns (text, filled).

    Only a bare entry is touched, and only when the skill's own front matter says what it
    is for: a description Claude Code already sent is never replaced, and a name with no
    file behind it is left as it came.
    """
    at = text.find(_LISTING_HEAD)
    if at < 0 or not descriptions:
        return text, 0
    head_end = at + len(_LISTING_HEAD)
    lines = text[head_end:].split("\n")
    filled, started = 0, False
    for i, line in enumerate(lines):
        entry = _ENTRY.match(line)
        if not entry:
            if started and line.strip():
                break
            continue
        started = True
        name = entry.group(1)
        if entry.group(2) is None and descriptions.get(name):
            lines[i] = f"- {name}: {descriptions[name]}"
            filled += 1
    return text[:head_end] + "\n".join(lines), filled


def listing(text: str) -> list[tuple[str, str]]:
    """(name, description) for every entry in the listing in `text`, in its order."""
    at = text.find(_LISTING_HEAD)
    if at < 0:
        return []
    entries, started = [], False
    for line in text[at + len(_LISTING_HEAD):].split("\n"):
        entry = _ENTRY.match(line)
        if not entry:
            if started and line.strip():
                break
            continue
        started = True
        entries.append((entry.group(1), (entry.group(2) or "").strip()))
    return entries


# --------------------------------------------------------------------------------------
# The bridge's own library
# --------------------------------------------------------------------------------------
#
# Skills adapted from Anthropic's Apache-2.0 plugins and skills (see skill_library/NOTICE),
# served to Agentaus in every project, including ones with no .claude/skills of their own.
# Claude Code has never heard of them, so they are listed and answered by the bridge: an
# entry is added to the listing, and a `Skill` call naming one is run by the bridge like
# any of its own tools. Claude sessions never see them.

LIBRARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill_library")

# Served on request rather than injected, so a library skill may be longer than an
# injected one - a review lens set or a phase-by-phase workflow does not fit in 9000.
_LIBRARY_MAX_CHARS = 40000


def library() -> dict[str, str]:
    """Every library skill, name -> description."""
    return {name: about for name, (about, _) in
            _load_root(LIBRARY, _LIBRARY_MAX_CHARS).items() if about}


def serve(name: str) -> str:
    """A library skill's procedure, framed as a loaded skill is - or why it cannot be."""
    loaded = _load_root(LIBRARY, _LIBRARY_MAX_CHARS).get((name or "").strip())
    if loaded is None:
        known = ", ".join(sorted(library())) or "(none)"
        return f"There is no library skill named {name!r}. Library skills: {known}."
    base = os.path.join(LIBRARY, name.strip())
    log.info("serving library skill: %s", name)
    return (f"Base directory for this skill: {base}\n\n"
            f"To open a file this procedure mentions (for example `references/...`), call "
            f"`agentaus_zoom` with its absolute path under that directory.\n\n"
            f"{loaded[1]}")


def add_entries(text: str, entries: list[tuple[str, str]], after: set[str]) -> tuple[str, int]:
    """`text` with "- name: description" lines added to its skill listing.

    Placed after the project's own skills - the most specific procedures stay first - and
    before the client's bundled ones, which are about Claude Code itself. A name already
    listed is skipped: the client serves its own, and a duplicate would be ambiguous.
    """
    at = text.find(_LISTING_HEAD)
    if at < 0 or not entries:
        return text, 0
    head_end = at + len(_LISTING_HEAD)
    lines = text[head_end:].split("\n")
    listed, first, last_project, end = set(), None, None, None
    for i, line in enumerate(lines):
        entry = _ENTRY.match(line)
        if not entry:
            if first is not None and line.strip():
                end = i
                break
            continue
        first = i if first is None else first
        listed.add(entry.group(1))
        if entry.group(1) in after:
            last_project = i
    if first is None:
        return text, 0
    new = [f"- {name}: {about}" for name, about in entries if name not in listed]
    if not new:
        return text, 0
    insert_at = (last_project + 1) if last_project is not None else first
    lines[insert_at:insert_at] = new
    return text[:head_end] + "\n".join(lines), len(new)

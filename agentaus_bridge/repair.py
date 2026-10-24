"""Tool calls Agentaus mangles in ways that can only have meant one thing.

Measured on the benchmark pilot, through the real Claude Code binary: the commonest
failures were not wrong decisions but wrong spellings of right ones. `agentaus_read` for
`Read`. `Python` for a script it wanted to run. `Bash` called with `cmd` instead of
`command`, `Write` with `path` instead of `file_path`, and relative paths that Claude
Code's file tools reject outright. Each one cost a correction round - and on a smaller
model a correction round is where the thread gets lost: in one run the model answered
its correction with a fragment of its own reasoning and the turn ended there.

Every repair here targets a tool that was actually offered, and only fills a field the
schema asks for and the call left empty. Anything ambiguous is left for the correction
path, which tells the model what was wrong.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re

from . import workflow_script
from .config import settings

log = logging.getLogger("agentaus-bridge")


def squash(name: str) -> str:
    return re.sub(r"[\s_\-.]", "", (name or "").lower())


# What a squashed invented name meant, most specific first. Only resolved when the
# target is on the wire.
_SYNONYMS = {
    "readfile": ("Read",), "openfile": ("Read",), "viewfile": ("Read",), "view": ("Read",),
    "cat": ("Read",), "fileread": ("Read",), "getfile": ("Read",), "read": ("Read",),
    "writefile": ("Write",), "createfile": ("Write",), "savefile": ("Write",),
    "filewrite": ("Write",), "newfile": ("Write",), "write": ("Write",),
    "editfile": ("Edit",), "replaceinfile": ("Edit",), "strreplace": ("Edit",),
    "strreplaceeditor": ("Edit",), "fileedit": ("Edit",), "edit": ("Edit",),
    "bash": ("Bash",), "shell": ("Bash",), "terminal": ("Bash",), "runcommand": ("Bash",),
    "run": ("Bash",), "execute": ("Bash",), "exec": ("Bash",), "command": ("Bash",),
    "runshell": ("Bash",), "shellcommand": ("Bash",), "executecommand": ("Bash",),
    "runbash": ("Bash",), "cmd": ("Bash",),
    "listfiles": ("Glob", "Bash"), "listdir": ("Glob", "Bash"), "ls": ("Glob", "Bash"),
    "findfiles": ("Glob",), "glob": ("Glob",),
    "grep": ("Grep",), "searchfiles": ("agentaus_search", "Grep"),
    "search": ("agentaus_search", "Grep"), "codesearch": ("agentaus_search", "Grep"),
    "websearch": ("agentaus_web_search", "WebSearch"), "searchweb": ("agentaus_web_search",),
    "fetch": ("WebFetch",), "webfetch": ("WebFetch",), "fetchurl": ("WebFetch",),
}

# Names meaning "run this Python", which Claude Code has no tool for: Bash runs it.
_PYTHON = {"python", "python3", "runpython", "executepython", "pythoninterpreter",
           "codeinterpreter", "runcode", "executecode", "ipython", "jupyter"}
for _name in _PYTHON:
    _SYNONYMS[_name] = ("Bash",)

# Prefixes a model puts in front of a real name: `agentaus_read`, `functions.Read`.
_PREFIX = re.compile(r"^(?:agentaus|functions|function|tools|tool|claude|default_api)[._\-:]+",
                     re.I)


def resolve(name: str, known: set) -> str | None:
    """The offered tool an unrecognised `name` can only have meant, or None."""
    squashed = squash(name)
    by_squash = {squash(k): k for k in known}
    if squashed in by_squash:
        return by_squash[squashed]
    stripped = _PREFIX.sub("", name or "")
    if stripped != name:
        hit = resolve(stripped, known)
        if hit:
            return hit
    for target in _SYNONYMS.get(squashed, ()):
        if target in known:
            return target
    return None


# Field names models use for the one the schema wants. Only applied when the canonical
# field is absent and the alias is not itself a field of that tool.
_ALIASES = {
    "command": ("cmd", "script", "code", "shell", "bash", "command_line", "commandline",
                "commands", "input", "query", "run"),
    "file_path": ("path", "filepath", "filename", "file", "file_name", "target_file",
                  "target", "absolute_path", "fullpath", "full_path"),
    "content": ("text", "contents", "data", "body", "file_content", "file_contents",
                "source", "code"),
    "old_string": ("old", "old_text", "old_str", "search", "find", "original", "before",
                   "target_text"),
    "new_string": ("new", "new_text", "new_str", "replace", "replacement", "after"),
    "pattern": ("glob", "regex", "query", "search", "name_pattern", "path_pattern"),
    "url": ("link", "href", "address", "uri"),
}

# Tools whose `file_path` Claude Code requires to be absolute.
_ABSOLUTE = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}


def _into_working_directory(path: str, cwd: str, *, must_exist: bool) -> str | None:
    """The same path under the real working directory, when the model garbled its prefix.

    Long sandbox paths get spliced: observed, a session whose working directory was
    `/private/tmp/claude-501/-Users-.../scratchpad/runs/wf1` wrote its answer to
    `/private/tmp/claude-501/-private-tmp-claude-501--Users-.../scratchpad/runs/wf1/` -
    the session-scratchpad prefix glued to the working directory's tail. The write
    "succeeded" into a directory tree that did not exist, and the deliverable was lost.

    Moved only when it can only have meant the working directory: the directory named
    does not exist, at least its last two components match the working directory's
    last two, and the same file under the working directory is where it is expected.
    """
    if not os.path.isabs(path) or not os.path.isdir(cwd):
        return None
    if os.path.isdir(os.path.dirname(path)):
        return None
    root = os.path.normpath(cwd)
    # A path under the working directory is the model's own choice - a new
    # subdirectory, say - however much it repeats the directory's name.
    if os.path.normpath(path).startswith(root + os.sep):
        return None
    parts = [p for p in os.path.normpath(path).split(os.sep) if p]
    tail = [p for p in root.split(os.sep) if p]
    if len(tail) < 2:
        return None
    for start in range(len(parts) - 2, -1, -1):
        if parts[start:start + 2] == tail[-2:]:
            # Only a garbled copy of the working directory is moved: some component of
            # the prefix must carry one of the working directory's own components inside
            # it, the way "-private-tmp-claude-501--Users-...-scratchpad" carries
            # "-Users-...-scratchpad". A real directory elsewhere - a backup volume, a
            # sibling project - shares no such fragment and is left alone.
            prefix = parts[:start]
            own = {c for c in tail[:-2] if len(c) >= 6}
            if not any(c != frag and frag in c for c in prefix for frag in own):
                return None
            rest = parts[start + 2:]
            if not rest:
                return None
            candidate = os.path.join(cwd, *rest)
            if os.path.normpath(candidate) == os.path.normpath(path):
                return None
            if must_exist and not os.path.exists(candidate):
                return None
            if not must_exist and not os.path.isdir(os.path.dirname(candidate)):
                return None
            return candidate
    return None


def _as_dict(arguments):
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments or "{}")
        except (ValueError, TypeError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _python_command(code: str) -> str:
    fence = "PY_EOF"
    while fence in code:
        fence += "_"
    return f"python3 - <<'{fence}'\n{code.rstrip()}\n{fence}"


# --- Edit old_string, against the file on disk --------------------------------------------
#
# Measured (d12 c1913h): every post-graph Edit's old_string used 8 spaces where the file had 4,
# Claude Code refused each with "String to replace not found", and the run shipped 0.5 where
# the same file with line 69 fixed grades 1.0. Edit notices in 6 post-graph turns of d9-d13.
# The bridge runs on the machine the file is on, so it can match the call against the file.

# Characters a model types for the ones in the file, all one-for-one so offsets survive.
_UNI = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
                      "\u2212": "-", "\u00a0": " ", "\u202f": " ", "\u2018": "'",
                      "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_LINENO = re.compile(r"^ *\d+\t", re.M)       # a Read prefix pasted into old_string
_EDIT_FILE_BYTES = 2_000_000
APPLIED = "applied"


def _lead(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _block_at(text: str, lines: list) -> list:
    """Line indexes where `lines` (compared stripped of indentation and trailing space)
    start in `text`."""
    key = [l.translate(_UNI).strip() for l in lines]
    if not any(key):
        return []
    tl = text.split("\n")
    return [i for i in range(len(tl) - len(lines) + 1)
            if all(tl[i + k].translate(_UNI).strip() == key[k] for k in range(len(lines)))]


def _near_block(text: str, lines: list) -> list:
    """[(line index, {k: file line})] where `lines` match `text` line for line but for a few
    lines that differ by a word or two - a model retyping a docstring "their index" as "the
    index". At least three non-blank lines, at most one in five differing, each differing
    line at least 85% alike."""
    key = [l.translate(_UNI).strip() for l in lines]
    body = [k for k in key if k]
    if len(body) < 3:
        return []
    allow = max(1, len(body) // 5)
    tl = text.split("\n")
    out = []
    for i in range(len(tl) - len(lines) + 1):
        diff = {}
        for k, want in enumerate(key):
            have = tl[i + k].translate(_UNI).strip()
            if have == want:
                continue
            if not want or not have or len(diff) >= allow or \
                    difflib.SequenceMatcher(None, want, have).ratio() < 0.85:
                diff = None
                break
            diff[k] = tl[i + k]
        if diff:
            out.append((i, diff))
    return out


def _detab(block: str) -> str:
    """Leading tabs as four spaces each, for a model that indents with tabs where the file
    has spaces (d15 he10: every Edit of order_by_points failed)."""
    return "\n".join(_lead(l).replace("\t", "    ") + l.lstrip(" \t") for l in block.split("\n"))


def _is_long(new: str) -> bool:
    return sum(1 for l in new.split("\n") if l.strip()) >= 2 or len(new.strip()) >= 30


def repair_edit(text: str, old: str, new: str):
    """(old, new, how) that applies where the model meant, (APPLIED, line, how) when the edit
    is already in the file, or None when there is no single answer.

    Only a unique match is used: a Read line prefix pasted in, typographic characters typed
    as ASCII, or the whole block shifted by one indentation step - every line by the same
    number of spaces, so the new text is shifted with it. Tabs are never converted.
    """
    if not old or text.count(old) >= 1:
        return None
    how = []
    if all(_LINENO.match(l) for l in old.split("\n") if l.strip()):
        old, new = _LINENO.sub("", old), _LINENO.sub("", new)
        how.append("Read line prefix removed")
        if text.count(old) == 1:
            return old, new, ", ".join(how)
    flat = text.translate(_UNI)
    if flat.count(old.translate(_UNI)) == 1:
        at = flat.index(old.translate(_UNI))
        return text[at:at + len(old)], new, ", ".join(how + ["typographic characters"])
    if any("\t" in _lead(l) for l in old.split("\n")) and not re.search(r"^ *\t", text, re.M):
        old, new = _detab(old), _detab(new)
        how.append("tabs as four spaces")
        if text.count(old) == 1:
            return old, new, ", ".join(how)
    first_nl = len(old) - len(old.lstrip("\n"))
    last_nl = len(old) - len(old.rstrip("\n"))
    lines = old.strip("\n").split("\n")
    hits = _block_at(text, lines)
    if not hits:
        near = _near_block(text, lines)
        if len(near) == 1:
            # The file's words, in the old text and wherever the new text kept the slipped line.
            at, diff = near[0]
            slipped = {lines[k].translate(_UNI).strip(): real.strip() for k, real in diff.items()}
            lines = [_lead(l) + diff[k].strip() if k in diff else l for k, l in enumerate(lines)]
            new = "\n".join(_lead(l) + slipped[l.translate(_UNI).strip()] if l.translate(_UNI).strip() in slipped else l
                            for l in new.split("\n"))
            how.append(f"{len(diff)} line(s) retyped with a slip, taken from the file")
            hits = [at]
    if len(hits) == 1:
        real = text.split("\n")[hits[0]:hits[0] + len(lines)]
        pairs = [(o, r) for o, r in zip(lines, real) if o.strip()]
        if any("\t" in _lead(o) + _lead(r) for o, r in pairs):
            return None
        delta = len(_lead(pairs[0][1])) - len(_lead(pairs[0][0]))
        if any(len(_lead(r)) - len(_lead(o)) != delta for o, r in pairs):
            return None

        def shift(line: str) -> str:
            if not line.strip():
                return line
            return " " * delta + line if delta > 0 else line[min(-delta, len(_lead(line))):]

        fixed_old = "\n" * first_nl + "\n".join(real) + "\n" * last_nl
        if text.count(fixed_old) != 1:
            return None
        return (fixed_old, "\n".join(shift(l) for l in new.split("\n")),
                ", ".join(how + [f"indentation {delta:+d}"]))
    if hits:
        return None
    # Not in the file in any form. If the new text is, uniquely and at length, the edit was
    # made already (d13 c1913h re-applied one). A short new_string proves nothing: "return
    # None" is in most files.
    if new and _is_long(new):
        if text.count(new) == 1:
            return APPLIED, text[:text.index(new)].count("\n") + 1, "already in the file"
        placed = _block_at(text, new.strip("\n").split("\n"))
        if len(placed) == 1:
            return APPLIED, placed[0] + 1, "already in the file"
    return None


def _file_text(path: str, pending: dict | None) -> str | None:
    if pending is not None and path in pending:
        return pending[path]
    try:
        if os.path.getsize(path) > _EDIT_FILE_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _repair_edits(name: str, args: dict, pending: dict | None) -> tuple[list, str | None]:
    """Repair an Edit's or MultiEdit's old_string against the file - or against the text the
    calls before it in this response leave, held in `pending` by path. In place. Returns
    (what changed, a correction for an edit that is already in the file)."""
    path = args.get("file_path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return [], None
    text = _file_text(path, pending)
    if text is None:
        return [], None
    edits = [args] if name == "Edit" else [e for e in args.get("edits") or [] if isinstance(e, dict)]
    changed = []
    for edit in edits:
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or text is None:
            text = None
            continue
        if edit.get("replace_all"):
            text = text.replace(old, new) if old and old in text else None
            continue
        fixed = repair_edit(text, old, new)
        if fixed and fixed[0] == APPLIED:
            return changed, (f"This edit is already in `{path}` (line {fixed[1]}): the file has "
                             "your new text and not the old. Do not apply it again; `Read` the "
                             "lines if you are unsure what is there.")
        if fixed:
            edit["old_string"], edit["new_string"] = old, new = fixed[0], fixed[1]
            changed.append(f"old_string: {fixed[2]}")
        text = text.replace(old, new, 1) if old and text.count(old) == 1 else None
    if pending is not None:
        if text is None:
            pending.pop(path, None)
        else:
            pending[path] = text
    return changed, None


_NOT_FOUND = re.compile(r"String to replace not found|String not found in file|old_string.{0,40}not found",
                        re.I)


def _closest_lines(text: str, old: str, n: int = 3) -> list:
    """(line number, line) of the `n` lines in `text` most like old_string's longest line."""
    probe = max((l.strip() for l in old.split("\n")), key=len, default="")
    if not probe:
        return []
    scored = []
    for number, line in enumerate(text.split("\n"), 1):
        if line.strip():
            ratio = difflib.SequenceMatcher(None, probe, line.strip()).quick_ratio()
            if ratio >= 0.5:
                scored.append((ratio, number, line))
    scored.sort(key=lambda r: -r[0])
    best = []
    for _, number, line in scored[: n * 4]:
        ratio = difflib.SequenceMatcher(None, probe, line.strip()).ratio()
        if ratio >= 0.6:
            best.append((ratio, number, line))
    best.sort(key=lambda r: -r[0])
    return sorted((number, line) for _, number, line in best[:n])


def edit_hints(body: dict) -> tuple[dict, int]:
    """`body` with the closest real lines added to each Edit refused as "not found" in the
    newest message - exact indentation shown - so the next try copies them instead of
    re-reading. Only for a call no repair could place (repair_edit gave no single answer)."""
    messages = list(body.get("messages") or [])
    if len(messages) < 2 or messages[-1].get("role") != "user":
        return body, 0
    content = messages[-1].get("content")
    previous = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), None)
    if not isinstance(content, list) or not previous or not isinstance(previous.get("content"), list):
        return body, 0
    uses = {b.get("id"): b for b in previous["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"
            and b.get("name") in ("Edit", "MultiEdit")}
    added, blocks = 0, []
    for block in content:
        if (isinstance(block, dict) and block.get("type") == "tool_result"
                and block.get("tool_use_id") in uses and _NOT_FOUND.search(_result_of(block))):
            args = uses[block["tool_use_id"]].get("input") or {}
            path = args.get("file_path") if isinstance(args, dict) else None
            text = _file_text(path, None) if isinstance(path, str) else None
            olds = [args.get("old_string")] + [e.get("old_string") for e in args.get("edits") or []
                                               if isinstance(e, dict)]
            old = next((o for o in olds if isinstance(o, str) and o and text and o not in text), None)
            near = _closest_lines(text, old) if text and old else []
            if near:
                width = max(len(str(n)) for n, _ in near)
                shown = "\n".join(f"{n:>{width}}: {line}    ({_describe_indent(line)})"
                                   for n, line in near)
                note = ("[bridge] old_string matches nothing in this file. The lines most like "
                        f"it, as they are now in `{path}` (line number, then the line with its "
                        f"exact indentation):\n```\n{shown}\n```\nCopy old_string from these "
                        "lines exactly - indentation included - and Edit again.")
                inner = block.get("content")
                inner = [{"type": "text", "text": inner}] if isinstance(inner, str) else list(inner or [])
                block = {**block, "content": inner + [{"type": "text", "text": note}]}
                added += 1
        blocks.append(block)
    if not added:
        return body, 0
    messages[-1] = {**messages[-1], "content": blocks}
    return {**body, "messages": messages}, added


def _describe_indent(line: str) -> str:
    lead = _lead(line)
    if not lead:
        return "no indentation"
    if set(lead) == {" "}:
        return f"{len(lead)} spaces"
    if set(lead) == {"\t"}:
        return f"{len(lead)} tab(s)"
    return f"indentation {lead!r}"


def _result_of(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content if isinstance(content, str) else ""


def repair(call: dict, asked_as: str, schema: dict | None, cwd: str | None,
           pending: dict | None = None) -> dict:
    """Return `call` with its arguments repaired where the meaning is unambiguous.

    `pending` (path -> text) carries what the Edit calls before this one in the same
    response leave in each file. An Edit that is already in the file comes back with a
    "correction" key: the message to send the model instead of forwarding the call."""
    args = _as_dict(call.get("arguments"))
    if args is None or not isinstance(schema, dict):
        return call
    properties = schema.get("properties") or {}
    name = call.get("name") or ""
    changed = []

    # "Run this Python" arrives as code, under whatever key; Bash wants a command.
    if name == "Bash" and squash(asked_as) in _PYTHON and "command" not in args:
        code = next((args[k] for k in ("code", "script", "source", "input", "program")
                     if isinstance(args.get(k), str)), None)
        if code:
            for k in ("code", "script", "source", "input", "program"):
                args.pop(k, None)
            args["command"] = _python_command(code)
            args.setdefault("description", "Run Python")
            changed.append(f"{asked_as}(code) -> Bash(python3)")

    for field, aliases in _ALIASES.items():
        if field not in properties or field in args:
            continue
        for alias in aliases:
            if alias in args and alias not in properties:
                value = args.pop(alias)
                if field == "command" and isinstance(value, list):
                    value = "\n".join(str(v) for v in value)
                args[field] = value
                changed.append(f"{alias} -> {field}")
                break

    if (name in _ABSOLUTE and cwd and isinstance(args.get("file_path"), str)
            and args["file_path"] and not os.path.isabs(args["file_path"])
            and not args["file_path"].startswith("~")):
        args["file_path"] = os.path.normpath(os.path.join(cwd, args["file_path"]))
        changed.append("relative file_path -> absolute")

    if name == "Workflow" and isinstance(args.get("script"), str):
        separated = workflow_script.separate_statements(args["script"])
        if separated != args["script"]:
            args["script"] = separated
            changed.append("workflow script: `}` run into the next statement -> `;`")

    if name in _ABSOLUTE and cwd and isinstance(args.get("file_path"), str):
        moved = _into_working_directory(args["file_path"], cwd, must_exist=name != "Write")
        if moved:
            args["file_path"] = moved
            changed.append("mangled working-directory path -> working directory")

    correction = None
    if name in ("Edit", "MultiEdit") and settings.agentaus_edit_repair:
        edited, correction = _repair_edits(name, args, pending)
        changed += edited

    if correction:
        log.info("edit already applied: %s", args.get("file_path"))
        return {**call, "correction": correction}
    if not changed:
        return call
    log.info("repaired %s call: %s", name, "; ".join(changed))
    return {**call, "arguments": json.dumps(args)}

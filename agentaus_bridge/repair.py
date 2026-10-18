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

import json
import logging
import os
import re

from . import workflow_script

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


def repair(call: dict, asked_as: str, schema: dict | None, cwd: str | None) -> dict:
    """Return `call` with its arguments repaired where the meaning is unambiguous."""
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

    if not changed:
        return call
    log.info("repaired %s call: %s", name, "; ".join(changed))
    return {**call, "arguments": json.dumps(args)}

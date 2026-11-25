"""Whether a turn that is about to end has actually finished the job.

Agentaus ends coding turns the way a junior hands in homework: the code is written, so
it must work. Read off benchmark transcripts, the misses that cost the most were not
ones it could not solve but ones it never checked - an edit followed by "Done", a test
run that still showed failures followed by a summary, a spec with eleven numbered
requirements of which eight were built, an answers file that was never written because
the turn ran out of time reading.

Every check here is structural - it reads the conversation's own tool calls and the
files on disk - so it costs no model call to decide. Only when one fires does the turn
cost one more call: the model is shown what is missing and asked to finish it.
"""

from __future__ import annotations

import os
import re
import shlex
from collections import OrderedDict

from .prompt_style import Template

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Source files: a change to one of these is a change to behaviour, which only running
# something can confirm. Data, docs and config edits are not gated.
CODE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".kt", ".kts", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs",
    ".swift", ".scala", ".sh", ".bash", ".zsh", ".lua", ".r", ".jl", ".pl", ".ex", ".exs",
    ".erl", ".hs", ".ml", ".dart", ".vue", ".svelte", ".sql",
}

# A Bash call made only of these looks at things; it runs nothing the edit changed.
_LOOK_ONLY = {
    "cat", "head", "tail", "less", "more", "ls", "ll", "tree", "grep", "egrep", "fgrep",
    "rg", "ag", "find", "fd", "wc", "echo", "printf", "pwd", "cd", "file", "stat", "du",
    "df", "which", "type", "diff", "cmp", "sort", "uniq", "cut", "tr", "awk", "sed", "nl",
    "true", "git", "mkdir", "touch", "cp", "mv", "rm", "chmod", "realpath", "basename",
    "dirname", "date", "export", "set", "column", "xxd", "od", "hexdump", "jq",
}
# Words that run the command after them: `env PYTHONPATH=src python -m pytest` runs pytest,
# so the word after them (and after their options and VAR=value settings) is the one judged.
_PREFIXES = {"env", "command", "exec", "time", "nice", "timeout", "sudo", "xargs"}

# A test runner's own summary of a failed run. Deliberately narrow: a program that
# prints "Error:" on purpose (an interpreter's error messages, a validator's output) must
# not read as a failure, so only the runners' summary lines count.
_FAILED_RUN = re.compile(
    r"(?m)(^=+ .*\b\d+ (?:failed|errors?)\b.* in [\d.]+s|^FAILED \(|^FAILED [\w/.-]+::"
    r"|^ERROR [\w/.-]+::|^E\s+\w*Error\b|^Tests:\s+.*\b\d+ failed|^FAIL\s|^--- FAIL:"
    r"|^\d+ failed, \d+ passed|^Ran \d+ tests? in .*\n+FAILED)"
)

# The request asking for a file to be produced: "Write `answers.json`", "save the
# result to report.md", "create a file `hello.py`".
_DELIVERABLE = re.compile(
    r"\b(?:write|create|save|produce|generate)\b"
    r"[^.,:;\n`]{0,40}?`([\w./-]+\.[A-Za-z0-9]{1,5})`",
    re.IGNORECASE,
)
# A dotted name is a file only with a file's extension (or a directory in it): "Write a test
# for `Parser.parse`", "a wrapper around `requests.get`" name code, and the gate would
# otherwise insist the model create them.
_FILE_EXTENSIONS = CODE_EXTENSIONS | {
    ".json", ".jsonl", ".md", ".txt", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".env", ".html", ".htm", ".css", ".scss", ".xml", ".svg", ".log",
    ".rst", ".adoc", ".ipynb", ".lock", ".out", ".patch", ".diff", ".db", ".sqlite",
    ".parquet", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".tar", ".gz", ".tgz", ".zip",
    ".bak", ".proto", ".graphql", ".tex", ".bib", ".dat", ".bin", ".pem",
}
# "Do not create `backup.tar`", "never write to `secrets.env`": a file the request forbids.
_NEGATED = re.compile(r"\b(?:not|never|don'?t|doesn'?t|avoid|without|no need)\b",
                      re.IGNORECASE)
_CLAUSE_END = re.compile(r"[.;:!?,\n]|\bbut\b", re.IGNORECASE)
# A request for a test that is meant to fail - "a failing test", "a test that reproduces
# the bug": its failing run is the deliverable. "Reproduce the bug, then fix it" is not.
_WANTS_FAILING = re.compile(
    r"\bfail(?:ing|s)\s+(?:\w+\s+){0,2}?tests?\b"
    r"|\b(?:tests?|test case|script)\s+(?:that|which|to)\s+(?:fails?|reproduc)"
    r"|\breproduc\w*\s+(?:\w+\s+){0,2}?tests?\b|\breproduces the (?:bug|issue|failure)\b",
    re.IGNORECASE)
# A document the request names as the authority: `SPEC.md`, `docs/RULES.md`.
_DOC = re.compile(r"`([\w./-]+\.(?:md|rst|adoc))`")
# Numbered list items, bullets, and the bold heads of quoted items (tickets, stories).
_NUMBERED = re.compile(r"^\s{0,6}(?:\d{1,2}[.)]|[-*+])\s+(\S.*)$")
_QUOTED_HEAD = re.compile(r"^\s{0,6}>\s*\*\*(.+?)\*\*")
_REQ_RANGE = re.compile(r"\b([A-Z]{1,3})(\d{1,2})\s*(?:[-–—]|to|through)\s*\1?(\d{1,2})\b")


def _as_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) and b.get("type") == "text"
            else (_as_text(b.get("content")) if isinstance(b, dict) else "")
            for b in content)
    return ""


_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def _is_user_text(message: dict) -> bool:
    """A user message that says something, rather than only returning tool results."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return False
    text = _REMINDER.sub("", text).strip()
    return bool(text) and "<task-notification>" not in text


def latest_request(messages: list) -> str:
    """The last thing the user actually said - the request this turn is answering. A
    skill's body, which arrives as user text after its `Skill` call, is not one."""
    latest, in_skill = "", False
    for m in messages:
        content = m.get("content")
        if m.get("role") == "assistant":
            in_skill = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill"
                for b in content)
            continue
        if in_skill or not _is_user_text(m):
            continue
        text = content if isinstance(content, str) else "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        latest = _REMINDER.sub("", text).strip()
    return latest


def turn_events(messages: list) -> list[tuple[str, dict, str, bool]]:
    """(tool name, input, result text, is_error) for each tool call of the current user
    turn, in order. The turn starts at the last thing the user actually said."""
    start = 0
    for i, m in enumerate(messages):
        if _is_user_text(m):
            start = i
    results = {}
    for m in messages[start:]:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = (_as_text(b.get("content")),
                                                     bool(b.get("is_error")))
    out = []
    for m in messages[start:]:
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                text, err = results.get(b.get("id"), ("", False))
                out.append((b.get("name") or "", b.get("input") or {}, text, err))
    return out


def _edited_path(name: str, args: dict) -> str:
    if name not in EDIT_TOOLS:
        return ""
    return str(args.get("file_path") or args.get("notebook_path") or "")


def is_code(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in CODE_EXTENSIONS


def _segments(command: str) -> list[list[str]]:
    """The simple commands of a shell line, split on operators outside quotes."""
    lexer = shlex.shlex(command or "", posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    out, current, redirect = [], [], False
    try:
        for token in lexer:
            if token and set(token) <= set(";&|()<>"):
                if "<" in token or ">" in token:
                    redirect = True  # `> file`, `2>&1`: the next word is a target
                    continue
                if current:
                    out.append(current)
                current = []
            elif redirect:
                redirect = False
            else:
                current.append(token)
    except ValueError:  # unbalanced quotes: fall back to a plain split
        return [seg.split() for seg in re.split(r"&&|\|\||[;|\n]", command or "")]
    if current:
        out.append(current)
    return out


def _command_words(words: list[str]) -> list[str]:
    """A simple command's words from the one that does the work: past `FOO=1`, `env`,
    `timeout 60`, `nice -n 5` and the like."""
    while words:
        head = os.path.basename(words[0])
        if re.match(r"^\w+=", words[0]):  # FOO=1 cmd
            words = words[1:]
        elif head in _PREFIXES:
            if head == "command" and any(w in ("-v", "-V") for w in words[1:2]):
                return []  # `command -v python` only looks the name up
            words = words[1:]
            while words and (words[0].startswith("-") or re.match(r"^\w+=", words[0])
                             or re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", words[0])):
                words = words[1:]  # options, settings, a timeout's duration, a niceness
        else:
            return words
    return words


def _segment_runs(words: list[str]) -> bool:
    words = _command_words(words)
    if not words:
        return False
    head = os.path.basename(words[0])
    if head == "find" and any(w in ("-exec", "-execdir", "-ok", "-okdir") for w in words):
        return True
    # `sed -i` changes a file but runs nothing; an in-place edit of code is counted as an
    # edit by _sed_edits instead.
    return head not in _LOOK_ONLY


def runs_something(command: str) -> bool:
    """Whether a shell command executes anything, rather than only looking at files."""
    return any(_segment_runs(words) for words in _segments(command))


def _sed_edits(words: list[str]) -> list[str]:
    """The code files a `sed -i` segment edits in place - an edit like any Edit call."""
    words = _command_words(words)
    if not words or os.path.basename(words[0]) != "sed":
        return []
    if not any(w.startswith("-i") or w.startswith("--in-place") for w in words[1:]):
        return []
    return [w for w in words[1:] if not w.startswith("-") and is_code(w)]


def verification_state(messages: list) -> dict:
    """What the turn changed and whether anything ran after the last change.

    Returns {"unrun": [code files edited since the last run], "failed": the output tail
    of the last run if it was a failed test run, "edits": number of code edits}."""
    unrun: "OrderedDict[str, None]" = OrderedDict()
    edits = 0
    last_run_output = None
    for name, args, result, err in turn_events(messages):
        path = _edited_path(name, args)
        if path and is_code(path):
            if err:  # refused ("String to replace not found"): nothing changed
                continue
            edits += 1
            unrun[path] = None
            last_run_output = None
        elif name == "Bash":
            # In order: `sed -i ... app.py && pytest` is verified, `pytest; sed -i` is not.
            for words in _segments(str(args.get("command") or "")):
                changed = _sed_edits(words)
                if changed:
                    edits += len(changed)
                    unrun.update((p, None) for p in changed)
                    last_run_output = None
                elif _segment_runs(words):
                    unrun.clear()
                    last_run_output = result
    failed = None
    if last_run_output and _FAILED_RUN.search(last_run_output):
        failed = last_run_output[-1500:]
    return {"unrun": list(unrun), "failed": failed, "edits": edits}


def deliverables(request: str) -> list[str]:
    """Files the request asks to be produced, in the order named."""
    seen = []
    request = request or ""
    for m in _DELIVERABLE.finditer(request):
        name = m.group(1)
        if "/" not in name and os.path.splitext(name)[1].lower() not in _FILE_EXTENSIONS:
            continue
        clause = request[:m.start(1)]
        ends = list(_CLAUSE_END.finditer(clause))
        if _NEGATED.search(clause[ends[-1].end() if ends else 0:]):
            continue
        if name not in seen:
            seen.append(name)
    return seen


def missing_deliverables(request: str, cwd: str | None, messages: list) -> list[str]:
    """Requested output files that do not exist yet. Read off the disk when the bridge
    can see the working directory; otherwise off the conversation's own writes."""
    wanted = deliverables(request)
    if not wanted:
        return []
    written = set()
    for name, args, _r, err in turn_events(messages):
        path = _edited_path(name, args)
        if path and not err:
            written.add(path)
        elif name == "Bash":
            written.add(str(args.get("command") or ""))
    missing = []
    for name in wanted:
        if cwd and os.path.isdir(cwd):
            if not os.path.exists(os.path.join(cwd, name)):
                missing.append(name)
        elif not any(name in w for w in written):
            missing.append(name)
    return missing


def checklist(request: str, *, limit: int = 14, width: int = 220) -> list[str]:
    """The request's own enumerated items: numbered points, bullets, quoted ticket heads."""
    items = []
    for line in (request or "").splitlines():
        m = _QUOTED_HEAD.match(line) or _NUMBERED.match(line)
        if not m:
            continue
        text = re.sub(r"\s+", " ", m.group(1)).strip().strip("*").strip()
        if len(text) < 12:
            continue
        if len(text) > width:
            text = text[:width].rsplit(" ", 1)[0] + " ..."
        items.append(text)
    return items[:limit]


def authority_docs(request: str, cwd: str | None) -> list[str]:
    """Documents the request names, that exist and are not the deliverable itself."""
    out = []
    produced = set(deliverables(request))
    for m in _DOC.finditer(request or ""):
        name = m.group(1)
        if name in produced or name in out:
            continue
        if cwd and os.path.isdir(cwd) and not os.path.exists(os.path.join(cwd, name)):
            continue
        out.append(name)
    return out[:4]


def requirement_range(request: str) -> str:
    m = _REQ_RANGE.search(request or "")
    return f"{m.group(1)}{m.group(2)}-{m.group(1)}{m.group(3)}" if m else ""


UNVERIFIED_CORRECTION = Template("""\
<correction>
You are about to finish, but you changed {files} and have run nothing since that change,
so you do not know that it works. Run it now with Bash: the project's tests if it has
them, and otherwise call the code you changed with real arguments (an import or a syntax
check is not enough - it does not execute the lines you edited), on the inputs and edge
cases the request describes. If
anything fails or prints something other than what the request specifies, fix it and run
it again. Only give your final answer once a run after your last edit shows it working.
</correction>""")

FAILED_CORRECTION = Template("""\
<correction>
You are about to finish, but the last test run after your changes still reports
failures:

```
{output}
```

The task is not done while these fail. Read the failure, find the cause in the code
(not in the test), fix it, and run the tests again. Only give your final answer once they
pass. If a failure really cannot be fixed, say exactly which one and why.
</correction>""")

DELIVERABLE_CORRECTION = Template("""\
<correction>
The request asks for {files}, and {it_they} not been written - nothing exists at that path
yet. Write it now with your tools, from what you have already worked out, in exactly the
format the request specifies. Then check it by reading it back.
</correction>""")

CHECKLIST_CORRECTION = Template("""\
<review>
Before you give your final answer, check the work against the request, item by item.
{items}{docs}
For each item you need evidence from this session that it is done: a command or test you
ran after your last change that exercises it, including the edge cases the request or
document spells out (exact messages, formats, boundaries, error cases). Do not re-read
your own summary - check the code and run it. If any item is missing, wrong or
unverified, fix or verify it now with your tools. If every item is done and verified,
reply with your final answer.
</review>""")


DELIVERABLE_NUDGE = Template("""\

--- Deliverable not written yet ---

The request asks for {files}. You have made {calls} tool calls in this turn and {it_they}
still not been written. Write {it_them} now from what you already know - you can keep
investigating and improve {it_them} afterwards. A turn that runs out of time without the
file delivers nothing at all, however much it read.
""")


def checklist_text(request: str, cwd: str | None) -> str:
    items = checklist(request)
    docs = authority_docs(request, cwd)
    rng = requirement_range(request)
    lines = "".join(f"- {t}\n" for t in items)
    head = "The request asked for:\n" + lines if lines else ""
    doc_line = ""
    if docs:
        which = ", ".join(f"`{d}`" for d in docs)
        doc_line = (f"\nThe request names {which} as the source of truth. Re-open "
                    f"{'it' if len(docs) == 1 else 'them'} now and go through "
                    + (f"every requirement {rng}" if rng else "every numbered rule and requirement")
                    + " in turn, checking each against the code.\n")
    return CHECKLIST_CORRECTION.format(items=head, docs=doc_line)


def wants_checklist(request: str, cwd: str | None) -> bool:
    return len(checklist(request)) >= 3 or bool(authority_docs(request, cwd))


def _quote_files(files: list[str], cwd: str | None) -> str:
    def rel(p: str) -> str:
        if cwd and p.startswith(cwd.rstrip("/") + "/"):
            return p[len(cwd.rstrip("/")) + 1:]
        return p
    names = [f"`{rel(f)}`" for f in files[:6]]
    return ", ".join(names) + (f" and {len(files) - 6} more" if len(files) > 6 else "")


def deliverable_nudge(request: str, cwd: str | None, messages: list, *,
                      after_calls: int) -> str:
    """A note for the system prompt once a turn has spent many calls without writing the
    file it was asked for. Empty when it does not apply."""
    if after_calls <= 0:
        return ""
    calls = len(turn_events(messages))
    if calls < after_calls:
        return ""
    missing = missing_deliverables(latest_request(messages) or request, cwd, messages)
    if not missing:
        return ""
    one = len(missing) == 1
    return DELIVERABLE_NUDGE.format(files=_quote_files(missing, cwd), calls=calls,
                                    it_they="it has" if one else "they have",
                                    it_them="it" if one else "them")


# Per conversation, across requests (Claude Code sends a fresh request after every tool
# call). Keyed like the stall budget: the original request plus the working directory.
_USED: "OrderedDict[str, dict]" = OrderedDict()
LIMITS = {"deliverable": 2, "unverified": 2, "failed": 2, "checklist": 1}


def _take(key: str, kind: str) -> bool:
    used = _USED.setdefault(key, {})
    if used.get(kind, 0) >= LIMITS[kind]:
        return False
    used[kind] = used.get(kind, 0) + 1
    _USED.move_to_end(key)
    while len(_USED) > 512:
        _USED.popitem(last=False)
    return True


def finish_gate(key: str, request: str, messages: list, cwd: str | None, *,
                can_run: bool, checklist_on: bool = True) -> tuple[str, str, bool]:
    """Decide whether a final answer should go back to the model.

    Returns (kind, correction, force_tool) - kind "" means let the answer through.

    `request` is the conversation's first message; the files asked for are read off the
    latest one, since a later "now write `notes.md`" is what this turn answers."""
    ask = latest_request(messages) or request
    missing = missing_deliverables(ask, cwd, messages)
    if missing and _take(key, "deliverable"):
        one = len(missing) == 1
        return ("deliverable", DELIVERABLE_CORRECTION.format(
            files=_quote_files(missing, cwd), it_they="it has" if one else "they have"), True)
    state = verification_state(messages)
    if can_run and state["unrun"] and _take(key, "unverified"):
        return ("unverified", UNVERIFIED_CORRECTION.format(
            files=_quote_files(state["unrun"], cwd)), True)
    # Not forced: "if it cannot be fixed, say why" must be answerable without a tool call.
    # And a request for a failing test is done when its run fails.
    if (can_run and state["failed"] and not _WANTS_FAILING.search(ask)
            and _take(key, "failed")):
        return ("failed", FAILED_CORRECTION.format(output=state["failed"].strip()), False)
    if (checklist_on and state["edits"] + len(deliverables(ask)) > 0
            and wants_checklist(request, cwd) and _take(key, "checklist")):
        return ("checklist", checklist_text(request, cwd), False)
    return ("", "", False)


REWRITE_NUDGE = Template("""\

--- Change files with Edit, not by rewriting them ---

{files} already exist{s} - you wrote {it_them} earlier in this turn. To change {it_them},
use Edit on the lines that change (several small Edits are fine). Do not Write the whole
file again: every full rewrite of a file this size takes minutes to generate, and
re-typing code that already works is where new mistakes come from. Rewrite only if the
file is genuinely unusable, and say why first.
""")


def rewrite_nudge(messages: list, cwd: str | None, *, min_chars: int = 4000) -> str:
    """A note for the system prompt once the turn has written a large file, so the next
    change to it is an Edit rather than a full rewrite. Empty when it does not apply.

    Measured (h_interpreter, base): four whole-file Writes of one 30 KB interpreter, at
    150-374 s each, took 17 of the turn's 25 minutes and it timed out testing."""
    big = OrderedDict()
    for name, args, _r, err in turn_events(messages):
        if name == "Write" and not err and len(str(args.get("content") or "")) >= min_chars:
            path = str(args.get("file_path") or "")
            if path:
                big[path] = None
    if not big:
        return ""
    files = list(big)
    one = len(files) == 1
    return REWRITE_NUDGE.format(files=_quote_files(files, cwd), s="s" if one else "",
                                it_them="it" if one else "them")

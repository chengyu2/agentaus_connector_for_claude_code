"""One client for every benchmark, so results are comparable across models.

Every suite talks to the local bridge rather than to a provider directly. That is the
whole reason a comparison is cheap here: the bridge routes on the request's `model`
field, so `--model agentaus` and `--model claude-opus-5` exercise the same harness, the
same prompts and the same parsing, and differ only in which model answered.

Telemetry is captured per call because a resolve rate on its own hides the interesting
part. A model that scores two points higher while spending four times the tokens and
three times the wall-clock has not won anything a production deployment cares about.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BRIDGE = os.environ.get("BENCH_BRIDGE_URL", "http://127.0.0.1:8787/v1/messages")


@dataclass
class Reply:
    text: str
    ok: bool
    seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""
    # Text from rounds that ended in a tool call ("let me look at gate.py"). Kept for the
    # record and never scored: a file the model only considered is not one it named.
    narration: str = ""
    # The model was still calling tools when TOOL_ROUNDS ran out, so `text` is whatever
    # it said on the way, not an answer it chose to give.
    exhausted: bool = False


@dataclass
class Totals:
    calls: int = 0
    failures: int = 0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    exhausted: int = 0
    latencies: list = field(default_factory=list)

    def add(self, reply: Reply) -> None:
        self.calls += 1
        self.failures += 0 if reply.ok else 1
        self.exhausted += 1 if reply.exhausted else 0
        self.seconds += reply.seconds
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        self.latencies.append(reply.seconds)

    @property
    def median_seconds(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        return ordered[len(ordered) // 2]


# The largest file body handed back to the model. A benchmark that lets one Read fill
# the window is measuring the window.
READ_LIMIT = 40_000

# Client-side tool rounds before the harness stops and takes what it has. The bridge
# runs its own tools internally; these are only the ones a suite offered.
TOOL_ROUNDS = 6


def _schema(name: str, description: str, properties: dict, required: list) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties,
                             "required": required}}


# The client tools a search suite offers every arm. Read alone left a model that cannot
# use the bridge's own search - any Claude arm, which passthrough forwards untouched -
# guessing paths, so a head-to-head measured which arm had a search tool, not which
# model searched better. Glob and Grep are what Claude Code itself would offer.
FILE_TOOLS = [
    _schema("Read", "Read a file from the filesystem.",
            {"file_path": {"type": "string"}}, ["file_path"]),
    _schema("Glob", "List files under a directory that match a glob pattern, such as "
            "**/*.py. The pattern is matched against each path relative to the directory "
            "and against the file name alone, so *.py finds .py files at any depth.",
            {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
    _schema("Grep", "Search file contents under a directory for a regular expression. "
            "Returns path:line: text for each match.",
            {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
]

_LISTING_LIMIT = 200
_GREP_LIMIT = 100
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def _confine(path: str, root: str | None) -> str | None:
    """`path` resolved against `root`, or None when it leaves it.

    A suite's answer key sits in this repository. An unconfined Read let a model open
    benchmarks/retrieval.py, or the README that quotes its first answer, and score on
    the key rather than on the search.
    """
    if not root:
        return os.path.abspath(path)
    base = os.path.realpath(root)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(base, path))
    return full if full == base or full.startswith(base + os.sep) else None


def _walk(base: str):
    for directory, subdirs, files in os.walk(base):
        subdirs[:] = sorted(d for d in subdirs if d not in _SKIP)
        for name in sorted(files):
            yield os.path.join(directory, name)


def _glob(arguments: dict, root: str | None) -> str:
    base = _confine(arguments.get("path") or root or ".", root)
    if base is None:
        return "That path is outside the folder this task is about."
    pattern = arguments.get("pattern") or "*"
    # fnmatch's * crosses "/", so **/*.py already reaches every depth except the top;
    # the stripped form covers that. Matching the bare file name as well is deliberate:
    # a model that asks for *.py means every Python file, not only those at the top,
    # and the schema says so.
    flat = pattern[3:] if pattern.startswith("**/") else pattern
    hits = [p for p in _walk(base)
            if fnmatch.fnmatch(os.path.relpath(p, base), pattern)
            or fnmatch.fnmatch(os.path.relpath(p, base), flat)
            or fnmatch.fnmatch(os.path.basename(p), pattern)]
    if not hits:
        return f"No files under {base} match {pattern!r}."
    shown = "\n".join(hits[:_LISTING_LIMIT])
    more = len(hits) - _LISTING_LIMIT
    return shown + (f"\n[{more} more not shown]" if more > 0 else "")


def _grep(arguments: dict, root: str | None) -> str:
    base = _confine(arguments.get("path") or root or ".", root)
    if base is None:
        return "That path is outside the folder this task is about."
    try:
        pattern = re.compile(arguments.get("pattern") or "", re.I)
    except re.error as exc:
        return f"Not a valid regular expression: {exc}"
    lines = []
    for path in ([base] if os.path.isfile(base) else _walk(base)):
        try:
            with open(path, "rb") as handle:
                raw = handle.read(1_000_000)
        except OSError:
            continue
        if b"\0" in raw:
            continue
        for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            if pattern.search(line):
                lines.append(f"{path}:{number}: {line.strip()[:200]}")
                if len(lines) >= _GREP_LIMIT:
                    return "\n".join(lines) + f"\n[stopped at {_GREP_LIMIT} matches]"
    return "\n".join(lines) or "No matches."


def _run_client_tool(name: str, arguments: dict, root: str | None = None) -> str:
    """Execute a tool the suite offered, the way Claude Code would.

    Anything not offered is reported back as unavailable rather than silently
    succeeding, so a model that invents a tool sees the same thing it would see in a
    real session.
    """
    arguments = arguments or {}
    if name == "Glob":
        return _glob(arguments, root)
    if name == "Grep":
        return _grep(arguments, root)
    if name != "Read":
        return f"No tool named {name!r} is available here."
    path = arguments.get("file_path") or ""
    if not path:
        return "Read needs a file_path."
    full = _confine(path, root)
    if full is None:
        return f"{path} is outside the folder this task is about."
    try:
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo not in sys.path:
            sys.path.insert(0, repo)
        from agentaus_bridge import tools as bridge_tools
        # Deliberately the bridge's reader, not open(): Claude Code's own Read shows a
        # .docx or .pdf as text, and a harness that hands back raw bytes would be
        # testing a client nobody uses.
        body = bridge_tools.read_text(full)
    except Exception as exc:
        return f"Could not read {path}: {type(exc).__name__}: {exc}"
    if not body:
        return f"{path} is empty or unreadable."
    if len(body) > READ_LIMIT:
        return body[:READ_LIMIT] + f"\n[truncated at {READ_LIMIT} characters]"
    return body


def ask(model: str, prompt: str, *, system: str = "", max_tokens: int = 2048,
        timeout: int = 600, tools: list | None = None, root: str | None = None) -> Reply:
    """A turn through the bridge, running any client tools it asks for.

    This loops rather than taking the first response, because a single-shot harness
    scores a tool call as an empty answer. That is not a model failure - the bridge
    handed back a perfectly good `tool_use` and the harness had nothing to do with it -
    and reading it as one measured the harness instead of the thing under test. It cost
    a coverage question a zero before anyone noticed, on a turn where the model had done
    everything right.
    """
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = [{"type": "text", "text": system}]
    if tools:
        body["tools"] = tools

    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    # A Claude arm goes through passthrough to api.anthropic.com, which needs a key of
    # its own. Claude Code normally supplies one; a harness has to be given it.
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        headers["x-api-key"] = key

    started = time.monotonic()
    spoken: list = []
    tokens_in = tokens_out = 0

    exhausted = False
    for _round in range(TOOL_ROUNDS + 1):
        request = urllib.request.Request(BRIDGE, data=json.dumps(body).encode(),
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            return Reply("", False, time.monotonic() - started,
                         error=f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            return Reply("", False, time.monotonic() - started,
                         error=f"{type(exc).__name__}: {exc}")

        if isinstance(data.get("error"), dict):
            return Reply("", False, time.monotonic() - started,
                         error=str(data["error"].get("message"))[:300])

        content = data.get("content") or []
        usage = data.get("usage") or {}
        tokens_in += int(usage.get("input_tokens") or 0)
        tokens_out += int(usage.get("output_tokens") or 0)
        spoken.append("".join(b.get("text", "") for b in content
                              if b.get("type") == "text"))

        wanted = [b for b in content if b.get("type") == "tool_use"]
        if not wanted:
            break
        if _round == TOOL_ROUNDS:
            exhausted = True
            break

        body["messages"] = body["messages"] + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.get("id") or "",
                    "content": _run_client_tool(call.get("name") or "",
                                                call.get("input") or {}, root),
                }
                for call in wanted
            ]},
        ]

    # Only the last round is the answer. Earlier rounds are narration before a tool call,
    # and scoring them credited a retrieval answer with every file the model mentioned
    # on its way to the one it chose.
    return Reply(spoken[-1] if spoken else "", True, time.monotonic() - started,
                 tokens_in, tokens_out, narration="\n".join(t for t in spoken[:-1] if t),
                 exhausted=exhausted)


def preflight(model: str) -> str:
    """Check a model answers at all, and say plainly why not. Returns "" when fine."""
    reply = ask(model, "Reply with the single word OK.", max_tokens=16, timeout=120)
    if reply.ok:
        return ""
    if "x-api-key" in reply.error or "authentication" in reply.error.lower():
        return (f"{model} needs an Anthropic key. The bridge forwards Claude requests to "
                f"api.anthropic.com, and a harness cannot borrow the OAuth token Claude "
                f"Code uses. Export ANTHROPIC_API_KEY and re-run.")
    return f"{model} did not answer: {reply.error}"


def record(reply: Reply, **fields) -> dict:
    """One case as it goes into the run file: the raw reply beside its score.

    Without the reply a score cannot be audited after the fact - nobody can tell a model
    failure from a scorer failure in a number alone.
    """
    return {**fields, "ok": reply.ok, "error": reply.error, "exhausted": reply.exhausted,
            "seconds": round(reply.seconds, 2), "reply": reply.text,
            "narration": reply.narration}


def answered(cases: list) -> list:
    """Cases the model actually answered. A call that failed - the bridge down, a quota
    spent - says nothing about the model, so rates are taken over the rest and failures
    are reported beside them."""
    return [c for c in cases if c.get("ok")]


def run_metadata() -> dict:
    """What a run was measured against, so two runs' numbers can be compared or not."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    meta: dict = {"bridge_url": BRIDGE}
    try:
        head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", repo, "status", "--porcelain",
                                "agentaus_bridge", "benchmarks"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
        meta["commit"] = head + ("+dirty" if dirty else "")
    except Exception as exc:
        meta["commit"] = f"unknown ({type(exc).__name__})"
    health = BRIDGE.split("/v1/", 1)[0] + "/healthz"
    try:
        with urllib.request.urlopen(health, timeout=10) as response:
            meta["bridge"] = json.load(response)
    except Exception as exc:
        meta["bridge"] = f"unavailable ({type(exc).__name__})"
    return meta

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

import json
import os
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


@dataclass
class Totals:
    calls: int = 0
    failures: int = 0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    latencies: list = field(default_factory=list)

    def add(self, reply: Reply) -> None:
        self.calls += 1
        self.failures += 0 if reply.ok else 1
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


def _run_client_tool(name: str, arguments: dict) -> str:
    """Execute a tool the suite offered, the way Claude Code would.

    Only `Read`, because that is the only one any suite offers. Anything else is
    reported back as unavailable rather than silently succeeding, so a model that
    invents a tool sees the same thing it would see in a real session.
    """
    if name != "Read":
        return f"No tool named {name!r} is available here."
    path = (arguments or {}).get("file_path") or ""
    if not path:
        return "Read needs a file_path."
    try:
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from agentaus_bridge import tools as bridge_tools
        # Deliberately the bridge's reader, not open(): Claude Code's own Read shows a
        # .docx or .pdf as text, and a harness that hands back raw bytes would be
        # testing a client nobody uses.
        body = bridge_tools.read_text(path)
    except Exception as exc:
        return f"Could not read {path}: {type(exc).__name__}: {exc}"
    if not body:
        return f"{path} is empty or unreadable."
    if len(body) > READ_LIMIT:
        return body[:READ_LIMIT] + f"\n[truncated at {READ_LIMIT} characters]"
    return body


def ask(model: str, prompt: str, *, system: str = "", max_tokens: int = 2048,
        timeout: int = 600, tools: list | None = None) -> Reply:
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

        body["messages"] = body["messages"] + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.get("id") or "",
                    "content": _run_client_tool(call.get("name") or "",
                                                call.get("input") or {}),
                }
                for call in wanted
            ]},
        ]

    return Reply("\n".join(t for t in spoken if t), True,
                 time.monotonic() - started, tokens_in, tokens_out)


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

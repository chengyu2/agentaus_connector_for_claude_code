#!/usr/bin/env python3
"""Read the bridge log and report what is actually going wrong, ranked.

The bridge compensates for a weaker upstream in a dozen places, and every one of those
compensations logs when it fires. That makes the log a record of which compensations are
earning their keep, which are firing so often they point at a real defect, and which
have never fired at all - and that is a far better guide to what needs fixing than
reading the code and guessing.

Usage:  ./scripts/diagnose.py [/tmp/agentaus-bridge.log]
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

# (label, pattern, severity, what it means, what to do about it)
SIGNALS = [
    ("upstream 5xx / gateway timeout",
     r"HTTP (5\d\d)|returned HTTP 52\d", "high",
     "Agentaus or Cloudflare failed the request outright.",
     "Expected under load. If frequent, lower AGENTAUS_SEARCH_CHUNK_TOKENS or "
     "AGENTAUS_MAX_CONCURRENCY - the adaptive ceiling handles bursts, not sustained load."),

    ("prompt rejected as too long",
     r"rejected the prompt as too long", "high",
     "The conversation exceeded Agentaus' window and had to be recompacted mid-turn.",
     "Lower AGENTAUS_COMPACT_THRESHOLD so compaction happens earlier."),

    ("tool round limit reached",
     r"tool round limit \(\d+\) reached", "high",
     "A turn used its whole tool budget without producing an answer.",
     "Raise AGENTAUS_TOOL_ROUNDS, or bound the task: the model explores indefinitely "
     "when the prompt does not say when to stop."),

    ("model invented a tool name",
     r"model invented \d+ tool name", "medium",
     "Agentaus called a tool nobody offered it. Corrected upstream, never surfaced.",
     "If frequent, the tool list in the prompt is being ignored - shorten it, or check "
     "the <tool_selection> block matches the wire."),

    ("model refused tools it has",
     r"refused to use tools it has", "high",
     "Agentaus claimed it cannot read files while holding tools that can.",
     "The re-ask usually recovers it. If frequent, strengthen the anti-refusal wording "
     "in TOOL_GUIDANCE."),

    ("repeated an identical tool call",
     r"repeated with identical arguments", "medium",
     "Agentaus re-ran a call it had already made. Answered from cache.",
     "Working as intended. High counts mean the tool ledger is not reaching the model."),

    ("chunk split after a timeout",
     r"timed out; splitting", "medium",
     "A search chunk was too slow and was halved rather than dropped.",
     "Working as intended. Sustained counts mean the configured chunk size is too big "
     "for this upstream's current load."),

    ("learned chunk ceiling lowered",
     r"capping search chunks at", "medium",
     "The bridge reduced its own chunk size after a capacity failure.",
     "Informational. If it never lifts again, the configured size is simply too high."),

    ("upstream call stalled",
     r"still waiting after", "high",
     "An upstream call produced nothing for long enough to be reported while it ran.",
     "A dead or wedged connection, not a slow answer. One of these held a run for 26 "
     "minutes while the upstream was healthy. If frequent, lower BRIDGE_READ_TIMEOUT so "
     "the retry happens sooner."),

    ("helper call abandoned on timeout",
     r"helper call exceeded", "high",
     "A bridge-initiated call hit AGENTAUS_HELPER_TIMEOUT and was abandoned.",
     "A stalled upstream. If frequent with AGENTAUS_STREAM_HELPERS=true, turn streaming off."),

    ("summarisation fell back to trimming",
     r"summarisation could not reach", "high",
     "Compaction could not summarise enough and dropped messages instead - real context lost.",
     "Lower AGENTAUS_KEEP_FRACTION, or compact earlier."),

    ("self-review revised the answer",
     r"self-review revised the answer", "low",
     "The review pass found a defect and rewrote the answer.",
     "Working as intended - this is the compensation paying off."),

    ("review verdict had to be adjudicated",
     r"review verdict was unstated", "low",
     "The reviewer ignored its output format and had to be asked again.",
     "One extra call. Frequent means REVIEW_INSTRUCTION's format is not landing."),

    ("planning pass failed",
     r"planning pass failed", "medium",
     "The turn proceeded without a plan.",
     "Usually an upstream blip. Frequent means the planning prompt is too large."),

    ("waited for a concurrency slot",
     r"waited [\d.]+s for a slot", "low",
     "A bridge call queued behind the bridge's own background work.",
     "Expected. Long waits mean call volume is too high for AGENTAUS_MAX_CONCURRENCY - "
     "prefer fewer, larger calls over raising the cap."),

    ("client disconnected mid-turn",
     r"client disconnected", "medium",
     "Claude Code gave up before the bridge answered.",
     "The turn was too slow. Check what preceded it - usually compaction or a long search."),

    ("stream closed early",
     r"stream closed early", "medium",
     "The response stream ended before it finished.",
     "Often the client hanging up. Paired with a disconnect, it is the same event."),

    ("in-band error from Agentaus",
     r"agentaus in-band error", "high",
     "Agentaus answered HTTP 200 with an error object instead of content.",
     "The bridge surfaces these correctly; read the message for the real cause."),

    ("distillation failed",
     r"distillation failed", "medium",
     "A tool result could not be condensed; the raw result was kept.",
     "Safe fallback. Frequent means AGENTAUS_DISTILL_CHUNK_TOKENS is too large."),

    ("search found nothing",
     r"search .*-> 0 of", "low",
     "A search read no chunks at all.",
     "Usually a path with nothing readable in it."),
]

TURN = re.compile(r"recv model=(\S+) route=(\w+)")
SEARCH = re.compile(r"bridge tool agentaus_\w+ ran in ([\d.]+)s")
COMPACT = re.compile(r"compaction done in ([\d.]+)s")


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/agentaus-bridge.log")
    if not path.exists():
        print(f"no log at {path}")
        return 1
    text = path.read_text(errors="replace")
    lines = text.splitlines()

    turns = TURN.findall(text)
    routes = Counter(r for _m, r in turns)
    print(f"=== {path}  ({len(lines):,} lines) ===\n")
    print(f"turns: {routes.get('agentaus', 0):,} to Agentaus, "
          f"{routes.get('anthropic', 0):,} passed through to Anthropic\n")

    tool_times = [float(t) for t in SEARCH.findall(text)]
    if tool_times:
        tool_times.sort()
        print(f"bridge tool calls: {len(tool_times)}  "
              f"median {tool_times[len(tool_times)//2]:.0f}s  "
              f"p90 {tool_times[int(len(tool_times)*0.9)]:.0f}s  "
              f"max {tool_times[-1]:.0f}s")
    compactions = [float(t) for t in COMPACT.findall(text)]
    if compactions:
        compactions.sort()
        print(f"compactions      : {len(compactions)}  "
              f"median {compactions[len(compactions)//2]:.1f}s  max {compactions[-1]:.0f}s")
    print()

    found = []
    for label, pattern, severity, means, action in SIGNALS:
        count = len(re.findall(pattern, text))
        if count:
            found.append((count, severity, label, means, action))

    order = {"high": 0, "medium": 1, "low": 2}
    found.sort(key=lambda row: (order[row[1]], -row[0]))

    print("=== what is firing ===\n")
    for count, severity, label, means, action in found:
        print(f"[{severity.upper():6}] {count:>5} x  {label}")
        print(f"                  {means}")
        print(f"                  -> {action}\n")

    silent = [label for label, pattern, *_ in SIGNALS if not re.search(pattern, text)]
    if silent:
        print("=== never fired (either healthy, or not exercised) ===")
        for label in silent:
            print(f"  - {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

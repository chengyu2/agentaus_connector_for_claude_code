#!/usr/bin/env python3
"""A/B benchmark: does the Agentaus compensation layer actually help?

Two independent verdicts per answer:

* **Execution.** The generated function is run against assertions covering the edge
  cases a weaker model tends to miss. Objective, and the one that settles it.

* **LLM-as-judge.** Agentaus scores the answer 1-5 for correctness, edge-case handling
  and clarity, without being told which arm produced it. Catches quality differences
  that a pass/fail cannot see - an answer can pass the tests while still being fragile.

Each task runs twice: once with compensation on, once off. The bridge is reconfigured
between arms, so the comparison is like-for-like on everything else.

    ./.venv/bin/python benchmarks/run.py                 # all tasks, both arms
    ./.venv/bin/python benchmarks/run.py --repeat 3      # average over runs
    ./.venv/bin/python benchmarks/run.py --tasks median,roman
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmarks.tasks import TASKS  # noqa: E402

BRIDGE = "http://127.0.0.1:8787"

JUDGE_PROMPT = """\
You are grading a candidate solution to a programming task. Be strict and objective.

TASK:
{task}

CANDIDATE SOLUTION:
{answer}

Score each dimension from 1 to 5:
- correctness: does it implement exactly what was asked?
- edge_cases: empty, zero, negative, duplicate, boundary and malformed inputs
- clarity: is it readable and free of unnecessary complexity?

Reply with ONLY a JSON object, no other text:
{{"correctness": N, "edge_cases": N, "clarity": N, "reason": "one short sentence"}}
"""


def ask(prompt: str, *, max_tokens: int = 900, timeout: int = 300) -> str:
    body = json.dumps({
        "model": "agentaus", "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(f"{BRIDGE}/v1/messages", data=body,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []))
    except urllib.error.HTTPError as e:
        return f"__ERROR__ {e.code} {e.read().decode()[:200]}"
    except Exception as e:
        return f"__ERROR__ {type(e).__name__}: {e}"


def extract_code(answer: str) -> str:
    """Pull the Python out of a reply that may be fenced or bare."""
    fenced = re.findall(r"```(?:python)?\s*\n(.*?)```", answer, re.S)
    if fenced:
        return max(fenced, key=len)
    return answer


def run_tests(code: str, tests: str, entry: str) -> tuple[bool, str]:
    """Execute the candidate against the task's assertions in a separate process.

    Subprocess rather than exec(): generated code can loop forever or exit, and neither
    should take the benchmark with it.
    """
    if entry not in code:
        return False, f"no function named {entry}"
    script = f"{code}\n\n{tests}\nprint('__PASS__')\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run([sys.executable, path], capture_output=True,
                              text=True, timeout=20)
        if "__PASS__" in proc.stdout:
            return True, ""
        err = (proc.stderr or proc.stdout).strip().splitlines()
        return False, (err[-1] if err else "failed")[:120]
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        Path(path).unlink(missing_ok=True)


def judge(task_prompt: str, answer: str) -> dict:
    raw = ask(JUDGE_PROMPT.format(task=task_prompt, answer=answer[:6000]),
              max_tokens=300)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {"correctness": 0, "edge_cases": 0, "clarity": 0, "reason": "unparseable"}
    try:
        d = json.loads(match.group(0))
        return {k: int(d.get(k, 0)) for k in ("correctness", "edge_cases", "clarity")} | \
               {"reason": str(d.get("reason", ""))[:80]}
    except Exception:
        return {"correctness": 0, "edge_cases": 0, "clarity": 0, "reason": "unparseable"}


def configure(guidance: bool, review: bool) -> bool:
    """Restart the bridge with the compensation layer on or off."""
    import os
    env = os.environ.copy()
    env["AGENTAUS_GUIDANCE"] = "true" if guidance else "false"
    env["AGENTAUS_SELF_REVIEW"] = "true" if review else "false"
    root = Path(__file__).resolve().parent.parent
    subprocess.run(["launchctl", "unload",
                    str(Path.home() / "Library/LaunchAgents/com.trellisdata.agentaus-bridge.plist")],
                   capture_output=True)
    subprocess.run(["pkill", "-f", "agentaus_bridge"], capture_output=True)
    proc = subprocess.Popen(
        [str(root / ".venv/bin/python"), "-m", "agentaus_bridge"],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import time
    for _ in range(40):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"{BRIDGE}/healthz", timeout=2):
                return True
        except Exception:
            continue
    proc.kill()
    return False


def restore_launchd() -> None:
    """Hand the port back to the launchd service.

    Without this the benchmark's own bridge keeps port 8787 and every later request -
    including a normal Claude Code session - silently hits a process started with
    benchmark settings. That happened once and looked like a quality regression.
    """
    import time
    subprocess.run(["pkill", "-f", "agentaus_bridge"], capture_output=True)
    time.sleep(1)
    subprocess.run(["launchctl", "load",
                    str(Path.home() / "Library/LaunchAgents/com.trellisdata.agentaus-bridge.plist")],
                   capture_output=True)
    time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()

    tasks = TASKS
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in TASKS if t["id"] in wanted]

    arms = [("compensated", True, True), ("baseline", False, False)]
    results: dict[str, list] = {name: [] for name, _, _ in arms}

    for name, guidance, review in arms:
        print(f"\n{'=' * 66}\n{name.upper()}  (guidance={guidance} self_review={review})\n{'=' * 66}")
        if not configure(guidance, review):
            print("  bridge failed to start")
            return 1
        for task in tasks:
            for run_i in range(args.repeat):
                answer = ask(task["prompt"])
                if answer.startswith("__ERROR__"):
                    print(f"  {task['id']:<16} ERROR {answer[:60]}")
                    results[name].append({"id": task["id"], "passed": False, "judge": None})
                    continue
                passed, detail = run_tests(extract_code(answer), task["tests"], task["entry"])
                scores = None if args.no_judge else judge(task["prompt"], answer)
                mark = "PASS" if passed else "FAIL"
                extra = ""
                if scores:
                    extra = (f"  judge c={scores['correctness']} "
                             f"e={scores['edge_cases']} cl={scores['clarity']}")
                suffix = f"  ({detail})" if not passed else ""
                print(f"  {task['id']:<16} {mark}{extra}{suffix}", flush=True)
                results[name].append({"id": task["id"], "passed": passed, "judge": scores})

    print(f"\n{'=' * 66}\nSUMMARY\n{'=' * 66}")
    print(f"{'arm':<14} {'pass rate':>12} {'correctness':>12} {'edge cases':>12} {'clarity':>10}")
    summary = {}
    for name in results:
        rows = results[name]
        if not rows:
            continue
        passed = sum(1 for r in rows if r["passed"])
        judged = [r["judge"] for r in rows if r["judge"]]
        avg = lambda k: (sum(j[k] for j in judged) / len(judged)) if judged else 0.0
        summary[name] = {
            "pass": passed / len(rows),
            "correctness": avg("correctness"),
            "edge": avg("edge_cases"),
            "clarity": avg("clarity"),
        }
        print(f"{name:<14} {passed}/{len(rows):<10} {avg('correctness'):>12.2f} "
              f"{avg('edge_cases'):>12.2f} {avg('clarity'):>10.2f}")

    if len(summary) == 2:
        c, b = summary["compensated"], summary["baseline"]
        delta = (c["pass"] - b["pass"]) * 100
        print(f"\ncompensation changes the pass rate by {delta:+.1f} points "
              f"and judged correctness by {c['correctness'] - b['correctness']:+.2f}")
        if delta < 0:
            print("NOTE: compensation scored WORSE here. That is a real result, not a bug.")

    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        restore_launchd()

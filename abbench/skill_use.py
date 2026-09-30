#!/usr/bin/env python3
"""Does the model reach for Skill and Agent when a task calls for them?

Real Claude Code sessions, headless, against a bridge on a test port. Each task names
the skill a careful model would load for it; the score is whether `Skill` was called
with that name, whether `Agent` was called and with which type, and which model served
any subagent - a subagent that silently runs on Claude is not an Agentaus subagent.

    python abbench/skill_use.py --arm base --port 8796 --runs 4
    python abbench/skill_use.py --arm opus --port 8796 --runs 1
    python abbench/skill_use.py --summary

Runs live under /Users/cheng/agentaus_ab_runs/skill_use, outside git and outside the
user's Claude memory, like the rest of abbench.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
RUNS = Path("/Users/cheng/agentaus_ab_runs/skill_use")
SMALL = Path("/Users/cheng/agentaus_ab_runs/_fixture_skills")

import sys  # noqa: E402

sys.path.insert(0, str(HERE))
from ab import CC, clean_env  # noqa: E402


@dataclass
class Task:
    id: str
    fixture: str          # "small" (four files) or "repo" (a clone of this repository)
    prompt: str
    skill: str            # the skill a careful model loads for this task


TASKS = [
    Task("find", "small", "Where is the retry backoff delay computed, and what is the "
         "longest it can ever wait? Point me to the code.", "find-in-code"),
    Task("survey", "small", "I've just been handed this folder. What's in it and what is "
         "it for?", "repo-survey"),
    Task("verify", "small", "docs/notes.md says the cache TTL is 300 seconds. Is that "
         "actually true of the code?", "investigate"),
    Task("docx", "small", "What requirements are listed in docs/requirements.docx, and "
         "which ones are only partially met?", "read-documents"),
    Task("flow", "repo", "Explain how one request travels through this bridge from "
         "arrival to response: which modules touch it, in what order. Cite files.",
         "repo-survey"),
    Task("every", "repo", "Find every place in this codebase that enforces a timeout or "
         "deadline, across all modules, and list them.", "search-exhaustively"),
    # Asked for outright: whether the model can drive `Agent` at all, and - the part that
    # matters for sovereignty - which model the subagent it starts actually runs on.
    Task("delegate", "repo", "Use the Explore agent to find where this bridge sets or "
         "enforces timeouts, then summarise what it found in a short list.", ""),
]

MODELS = {"opus": "claude-opus-5"}

# Whether the answer is right, which is what "used a skill appropriately" has to mean: a
# skill loaded for nothing costs a round, and one skipped where the tools already carry
# the same method costs nothing.
_FILES = ("server.py", "tools.py", "config.py", "documents.py", "translate.py")
CHECKS = {
    "find": lambda a: "retry.py" in a and bool(re.search(r"\b30(\.0)?\s*(s\b|sec|second)", a))
    and "urllib3" not in a,
    "survey": lambda a: all(k in a.lower() for k in ("retry", "config"))
    and ("requirements" in a.lower() or ".docx" in a.lower()),
    "verify": lambda a: "600" in a and not re.search(r"(?i)have not read|haven't read", a),
    "docx": lambda a: "R2" in a and "partial" in a.lower(),
    "flow": lambda a: sum(f in a for f in _FILES) >= 3,
    "every": lambda a: sum(f in a for f in _FILES) >= 3,
    "delegate": lambda a: "timeout" in a.lower() and sum(f in a for f in _FILES) >= 2,
}


def setup(task: Task, wd: Path) -> None:
    if task.fixture == "small":
        shutil.copytree(SMALL, wd, dirs_exist_ok=True)
    else:
        subprocess.run(["git", "clone", "-q", "--local", str(REPO), str(wd)], check=True)


def run_one(arm: str, port: int, task: Task, n: int) -> dict:
    wd = RUNS / arm / f"{task.id}-{n}"
    shutil.rmtree(wd, ignore_errors=True)
    wd.parent.mkdir(parents=True, exist_ok=True)
    setup(task, wd)
    model = MODELS.get(arm, "agentaus")
    settings = json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                                   "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus"},
                           "autoMemoryEnabled": False})
    cmd = [CC, "-p", "--model", model, "--settings", settings,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions", "--max-turns", "25",
           "--disallowedTools", "Edit", "Write", "NotebookEdit", "WebSearch", "WebFetch"]
    transcript = wd.parent / f"{task.id}-{n}.jsonl"
    started = time.time()
    with open(transcript, "w") as out:
        try:
            subprocess.run(cmd, cwd=wd, env=clean_env(port), input=task.prompt, text=True,
                           stdout=out, stderr=subprocess.DEVNULL, timeout=900)
        except subprocess.TimeoutExpired:
            pass
    rec = score(transcript, task)
    rec.update(arm=arm, task=task.id, n=n, wall=round(time.time() - started, 1))
    (wd.parent / f"{task.id}-{n}.json").write_text(json.dumps(rec, indent=1))
    shutil.rmtree(wd, ignore_errors=True)
    return rec


def score(transcript: Path, task: Task) -> dict:
    skills, agents, tools, sub_models, result = [], [], [], set(), ""
    for line in transcript.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            result = event.get("result") or ""
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        inside_subagent = bool(event.get("parent_tool_use_id"))
        if inside_subagent and message.get("model"):
            sub_models.add(message["model"])
        for block in message.get("content") or []:
            if block.get("type") != "tool_use":
                continue
            name, args = block.get("name", ""), block.get("input") or {}
            if inside_subagent:
                continue
            tools.append(name)
            if name.lower() == "skill":
                skills.append(args.get("skill") or args.get("name") or "?")
            elif name in ("Agent", "Task"):
                agents.append(args.get("subagent_type") or "general-purpose")
    correct = bool(result.strip()) and CHECKS.get(task.id, lambda a: True)(result)
    return {"expected": task.skill, "correct": correct,
            "skills": skills, "right_skill": task.skill in skills,
            "any_skill": bool(skills), "agents": agents, "subagent_models": sorted(sub_models),
            "tools": tools, "answered": bool(result.strip()), "result": result[:20000]}


def _answer(path: Path) -> str:
    """The final answer, re-read from the transcript - records once kept only 600 chars."""
    transcript = path.with_suffix(".jsonl")
    if transcript.exists():
        for line in reversed(transcript.read_text().splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                return event.get("result") or ""
    return json.loads(path.read_text()).get("result", "")


def _correct(rec: dict) -> bool:
    """Re-graded from the full answer, so a changed check applies to old runs too."""
    answer = rec.get("_answer", rec.get("result", ""))
    return bool(answer.strip()) and CHECKS.get(rec["task"], lambda a: True)(answer)


def summary() -> None:
    rows: dict[tuple, list] = {}
    for path in sorted(RUNS.glob("*/*.json")):
        rec = json.loads(path.read_text())
        rec["_answer"] = _answer(path)
        rows.setdefault((rec["arm"], rec["task"]), []).append(rec)
    arms = sorted({a for a, _ in rows})
    print(f"{'task':8} " + "  ".join(f"{a:>30}" for a in arms))
    for task in TASKS:
        cells = []
        for arm in arms:
            recs = rows.get((arm, task.id), [])
            if not recs:
                cells.append(f"{'-':>30}")
                continue
            ok = sum(_correct(r) for r in recs)
            anys = sum(r["any_skill"] for r in recs)
            agent = sum(bool(r["agents"]) for r in recs)
            wall = sorted(r["wall"] for r in recs)[len(recs) // 2]
            cells.append(f"{f'ok {ok}/{len(recs)} skill {anys} agent {agent} {wall:.0f}s':>30}")
        print(f"{task.id:8} " + "  ".join(cells))
    for arm in arms:
        recs = [r for (a, _), rs in rows.items() if a == arm for r in rs]
        models = sorted({m for r in recs for m in r["subagent_models"]})
        kinds = sorted({k for r in recs for k in r["agents"]})
        print(f"{arm}: agent types {kinds or '-'}; subagents served by {models or '-'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm")
    parser.add_argument("--port", type=int)
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--tasks", default=",".join(t.id for t in TASKS))
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    if args.summary:
        summary()
        return
    wanted = [t for t in TASKS if t.id in args.tasks.split(",")]
    jobs = [(t, n) for n in range(args.runs) for t in wanted]
    with cf.ThreadPoolExecutor(args.parallel) as pool:
        futures = [pool.submit(run_one, args.arm, args.port, t, n) for t, n in jobs]
        for future in cf.as_completed(futures):
            rec = future.result()
            print(f"{rec['arm']} {rec['task']}-{rec['n']}: skills={rec['skills']} "
                  f"agents={rec['agents']} sub={rec['subagent_models']} {rec['wall']}s",
                  flush=True)
    summary()


if __name__ == "__main__":
    main()

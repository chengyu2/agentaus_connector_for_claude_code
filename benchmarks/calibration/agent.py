"""The live half: run one task in a tool loop and grade what was actually done.

The loop is deliberately plain - the model, the task's tools, the answer format and
nothing else - so the only thing that differs between conditions is the system prompt.
Limits: MAX_TOOL_CALLS tool calls or MAX_SECONDS of working time, after which the model
is asked once for its final answer and confidence (that answer is graded). Working time
leaves out the API's retries - abandoned attempts and back-off waits - so a rate limit
does not use up a run's budget.
"""
from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import client, prompts
from .browser import Browser
from .metrics import parse_answer, parse_confidence
from .sandbox import Sandbox
from .tasks import SITE_HOST, Task, site

MAX_TOOL_CALLS = 30
MAX_SECONDS = 900
STEP_TOKENS = 32_768
SHUFFLE_SEED = 20261124
CONTINUE = "You ran out of space before finishing that step. Continue, more briefly."
AT_LIMIT = ("Stop working now: you have reached the limit on steps or time for this task. "
            "Do not call any more tools. Give your final response now, in the required "
            "format, including your confidence.")


def _fn(name: str, description: str, properties: dict, required: list) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


SANDBOX_TOOLS = [
    _fn("bash", "Run a bash command in /work (Python 3.12 is installed; there is no network). "
        "Returns stdout and stderr.", {"command": {"type": "string"}}, ["command"]),
    _fn("read_file", "Read a text file.", {"path": {"type": "string"}}, ["path"]),
    _fn("write_file", "Create or overwrite a text file with the given content.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
]
BROWSER_TOOLS = [
    _fn("open_url", "Open a URL in a text-mode browser and return the page as text with "
        "numbered links.", {"url": {"type": "string"}}, ["url"]),
    _fn("follow_link", "Follow link [n] on the current page.",
        {"number": {"type": "integer"}}, ["number"]),
]


def _execute(call: dict, sandbox: Sandbox | None, browser: Browser | None) -> str:
    name = call["function"]["name"]
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except json.JSONDecodeError as exc:
        return f"[invalid JSON arguments: {exc}]"
    try:
        if sandbox and name == "bash":
            return sandbox.bash(str(args["command"]))
        if sandbox and name == "read_file":
            return sandbox.read_file(str(args["path"]))
        if sandbox and name == "write_file":
            return sandbox.write_file(str(args["path"]), str(args["content"]))
        if browser and name == "open_url":
            return browser.open(str(args["url"]))
        if browser and name == "follow_link":
            return browser.follow(int(args["number"]))
    except (KeyError, TypeError, ValueError) as exc:
        return f"[bad arguments for {name}: {exc}]"
    except OSError as exc:              # e.g. write_file onto a path the agent made unwritable
        return f"[{name} failed: {exc}]"
    return f"[unknown tool {name!r}]"


def run_task(task: Task, condition: str) -> tuple[dict, list]:
    """Run one task under one condition. Returns (graded row, transcript)."""
    sandbox = Sandbox(task.files) if task.kind != "browser" else None
    browser = Browser(site(), SITE_HOST) if task.kind == "browser" else None
    tools = SANDBOX_TOOLS if sandbox else BROWSER_TOOLS
    messages = [{"role": "system", "content": prompts.system_prompt(condition, prompts.agent_format(task.kind))},
                {"role": "user", "content": task.prompt}]
    started = time.monotonic()
    stats = {"steps": 0, "tool_calls": 0, "truncations": 0, "input_tokens": 0,
             "output_tokens": 0, "cost_aud": 0.0, "hit_limit": False, "reasked": False,
             "final_path": "direct", "waited_seconds": 0.0}
    final = ""

    def count(reply):
        stats["steps"] += 1
        stats["input_tokens"] += reply.input_tokens
        stats["output_tokens"] += reply.output_tokens
        stats["cost_aud"] += reply.cost_aud
        stats["waited_seconds"] += reply.waited_seconds

    def finish(prompt: str) -> str:
        messages.append({"role": "user", "content": prompt})
        replies, stats["final_path"] = client.ask_final(messages, tools=tools)
        for reply in replies:
            count(reply)
        messages.append(replies[-1].as_message())
        return replies[-1].content or ""

    try:
        while True:
            if (stats["tool_calls"] >= MAX_TOOL_CALLS
                    or time.monotonic() - started - stats["waited_seconds"] >= MAX_SECONDS):
                stats["hit_limit"] = True
                final = finish(AT_LIMIT)
                break
            reply = client.chat(messages, max_tokens=STEP_TOKENS, tools=tools)
            count(reply)
            messages.append(reply.as_message())
            if reply.tool_calls:
                for call in reply.tool_calls:
                    stats["tool_calls"] += 1
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": _execute(call, sandbox, browser)})
                continue
            if reply.truncated:
                stats["truncations"] += 1
                messages.append({"role": "user", "content": CONTINUE})
                continue
            final = reply.content or ""
            if parse_confidence(final) is None:
                stats["reasked"] = True          # stopped without the format
                final = finish(prompts.REASK) or final
            break

        # Only the Answer line is graded: a "could not" in the Explanation must not pass a
        # cannot-determine grader. A reply with no Answer line has no answer.
        answer = parse_answer(final)
        stats["answer_missing"] = not answer
        if task.kind == "coding":
            sandbox.add_hidden(task.hidden)
            out = sandbox.bash("python -m unittest -q tests.test_hidden 2>&1 | tail -3", timeout=120)
            correct = out.rstrip().endswith("OK") or "\nOK" in out
            stats["hidden_tests"] = out[-300:]
        else:
            correct = bool(task.grade_answer(answer))
    finally:
        if sandbox:
            sandbox.close()

    confidence = parse_confidence(final)
    row = {"id": task.id, "kind": task.kind, "condition": condition, "correct": int(correct),
           "confidence_missing": confidence is None,
           "confidence": 1.0 if confidence is None else confidence,
           "answer": answer[:500], "seconds": time.monotonic() - started,
           "working_seconds": time.monotonic() - started - stats["waited_seconds"], **stats}
    return row, messages


def run(tasks: list[Task], conditions: list[str], out_dir: Path, *, workers: int = 4,
        repeat: int | dict = 1) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "transcripts").mkdir(exist_ok=True)
    jobs = []
    for condition in conditions:
        path = out_dir / f"live_{condition}.jsonl"
        done = set()
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                done.add((row["id"], row.get("repeat", 0)))
        times = repeat.get(condition, 1) if isinstance(repeat, dict) else repeat
        for r in range(times):
            jobs += [(t, condition, r, path) for t in tasks if (t.id, r) not in done]
    # Interleave conditions, so time of day and API load are not confounded with condition.
    random.Random(SHUFFLE_SEED).shuffle(jobs)

    def work(job):
        task, condition, r, path = job
        try:
            row, transcript = run_task(task, condition)
        except Exception as exc:  # noqa: BLE001 - one failed run must not stop the rest
            print(f"  {condition} {task.id}: failed ({exc})", flush=True)
            return
        row["repeat"] = r
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        (out_dir / "transcripts" / f"{condition}__{task.id}__{r}.json").write_text(
            json.dumps(transcript, indent=1))
        print(f"  {condition:<10} {task.id:<15} correct={row['correct']} "
              f"conf={row['confidence']:.2f} calls={row['tool_calls']} "
              f"limit={row['hit_limit']} {row['cost_aud']:.3f} AUD", flush=True)

    print(f"live: {len(jobs)} runs to do", flush=True)
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(work, jobs))

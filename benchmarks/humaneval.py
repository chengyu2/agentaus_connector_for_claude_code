"""HumanEval, pass@1, run locally.

164 problems, each with the canonical unit tests that ship with the dataset - so the
verdict is the dataset's, not this harness's opinion. Downloaded once and cached.

Generated code is executed in a subprocess with a wall-clock timeout and no arguments,
which bounds a runaway loop and an accidental `while True`. It is NOT a sandbox: a
determined program could still touch the filesystem or the network. Run it on a machine
where that is acceptable, or inside a container.
"""
from __future__ import annotations

import gzip
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/openai/human-eval/master/data/HumanEval.jsonl.gz"
CACHE = Path(__file__).parent / "data" / "HumanEval.jsonl.gz"

SYSTEM = ("You are a Python programmer. Output only code - no prose, no explanation, no "
          "markdown fence. Your code must define the requested function exactly as named.")

PROMPT = """Complete this Python function.

<function>
{prompt}
</function>

<output_format>
The complete function, including its signature and body. Python only, nothing else.
</output_format>"""


def problems(limit: int | None = None) -> list[dict]:
    if not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=120) as response:
            CACHE.write_bytes(response.read())
    rows = [json.loads(line) for line in gzip.open(CACHE, "rt")]
    return rows[:limit] if limit else rows


_FENCE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)


def extract_code(text: str) -> str:
    """The code a model meant to give, whatever it wrapped it in."""
    fenced = _FENCE.findall(text or "")
    if fenced:
        return max(fenced, key=len).strip()
    return (text or "").strip()


def passes(problem: dict, code: str, timeout: int = 15) -> tuple[bool, str]:
    """Run the dataset's own tests against the generated code."""
    program = (
        "\n".join([
            problem["prompt"].split("def ")[0],       # imports from the stub, if any
            code,
            problem["test"],
            f"check({problem['entry_point']})",
        ])
    )
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "candidate.py"
        path.write_text(program)
        try:
            done = subprocess.run([sys.executable, str(path)], capture_output=True,
                                  timeout=timeout, cwd=workdir)
        except subprocess.TimeoutExpired:
            return False, "timeout"
    if done.returncode == 0:
        return True, ""
    tail = done.stderr.decode("utf-8", "replace").strip().splitlines()
    return False, (tail[-1] if tail else "non-zero exit")[:160]


def run(model: str, limit: int | None, ask, totals, log=print) -> dict:
    rows = problems(limit)
    solved, failures = 0, []
    for index, problem in enumerate(rows, 1):
        reply = ask(model, PROMPT.format(prompt=problem["prompt"]), system=SYSTEM)
        totals.add(reply)
        if not reply.ok:
            failures.append((problem["task_id"], f"call failed: {reply.error[:80]}"))
            log(f"  [{index}/{len(rows)}] {problem['task_id']:16} CALL FAILED")
            continue
        ok, why = passes(problem, extract_code(reply.text))
        solved += ok
        if not ok:
            failures.append((problem["task_id"], why))
        log(f"  [{index}/{len(rows)}] {problem['task_id']:16} "
            f"{'pass' if ok else 'FAIL'}  {reply.seconds:5.1f}s  {'' if ok else why[:60]}")
    return {"suite": "humaneval", "n": len(rows), "solved": solved,
            "pass_at_1": solved / max(1, len(rows)), "failures": failures}

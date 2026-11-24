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


_FENCE = re.compile(r"```(?:[\w+-]*[^\S\n]*\n)?(.*?)```", re.S)


def extract_code(text: str, entry_point: str = "") -> str:
    """The code a model meant to give, whatever it wrapped it in.

    With several fenced blocks, the one that defines the target wins, and the longest
    only when none does: a reply that ends with a long block of example calls or tests
    used to have those run as the answer. Any language tag is dropped - ```py and
    ```python3 used to leave "py" or "3" as the first line of the program.
    """
    fenced = _FENCE.findall(text or "")
    if fenced:
        target = re.compile(rf"def {re.escape(entry_point)}\s*\(") if entry_point else None
        defining = [b for b in fenced if target and target.search(b)]
        return max(defining or fenced, key=len).strip()
    return (text or "").strip()


def passes(problem: dict, code: str, timeout: int = 15) -> tuple[bool, str]:
    """Run the dataset's own tests against the generated code."""
    # The FULL prompt goes first, not just its imports. Several problems define a helper
    # the solution must call - HumanEval/32 supplies `poly` for `find_zero`, /38 supplies
    # `encode_cyclic` for `decode_cyclic` - and keeping only the text before the first
    # `def` threw those away. Three of the first four failures in a real run were this
    # harness discarding a helper, not a model getting the answer wrong.
    #
    # A prompt is valid Python on its own: it ends with the target's signature and a
    # docstring, which is a legal body. The model's definition then shadows that stub.
    program = "\n".join([
        problem["prompt"],
        "",
        code,
        "",
        problem["test"],
        f"check({problem['entry_point']})",
    ])
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
    import harness

    rows = problems(limit)
    cases = []
    for index, problem in enumerate(rows, 1):
        reply = ask(model, PROMPT.format(prompt=problem["prompt"]), system=SYSTEM)
        totals.add(reply)
        if not reply.ok:
            cases.append(harness.record(reply, task_id=problem["task_id"]))
            log(f"  [{index}/{len(rows)}] {problem['task_id']:16} CALL FAILED")
            continue
        ok, why = passes(problem, extract_code(reply.text, problem["entry_point"]))
        cases.append(harness.record(reply, task_id=problem["task_id"], passed=ok, why=why))
        log(f"  [{index}/{len(rows)}] {problem['task_id']:16} "
            f"{'pass' if ok else 'FAIL'}  {reply.seconds:5.1f}s  {'' if ok else why[:60]}")
    # pass@1 over the problems the model was asked. A call that failed never reached the
    # model, so it is reported as a call failure rather than as a wrong answer.
    scored = harness.answered(cases)
    solved = sum(c["passed"] for c in scored)
    return {"suite": "humaneval", "n": len(rows), "answered": len(scored), "solved": solved,
            "pass_at_1": solved / max(1, len(scored)),
            "failures": [(c["task_id"], c.get("why") or c["error"][:80])
                         for c in cases if not c.get("passed")],
            "cases": cases}

#!/usr/bin/env python3
"""A/B benchmark: Claude Code driving Opus 5 vs Agentaus, through the real CLI binary.

Every run is one headless Claude Code session - the same native binary the VS Code
extension ships - in a fresh directory, graded afterwards with no model in the loop
except where stated.

    ./ab.py run --suite he --split dev --arms agentaus_tuned --label t1 --jobs 4
    ./ab.py report --label t1

Arms (the bridges are started by bridges.sh):
    opus            claude-opus-5 through the baseline bridge's passthrough (untouched)
    agentaus_base   agentaus through the baseline bridge  (main @ ceb597c) on 8795
    agentaus_tuned  agentaus through the tuned bridge     (this worktree)  on 8796

Suites:
    he        HumanEval+ : 164 problems, graded on the original tests AND the far
              stricter HumanEval+ inputs. Split dev (even ids) / test (odd ids): tune on
              dev, report on test, so the prompts are never fitted to the scored half.
    mmlu_pro  300 sampled MMLU-Pro questions, 10 per session
    mmlu      300 sampled MMLU questions, 10 per session
    econ_r    two economics tasks coded in R: an OLS model, and an end-to-end
              data.gov.au download plus a Pareto tail (rank-size) model

No lookups: coding and knowledge suites run with web tools off in every arm, and every
transcript is scanned for network commands afterwards.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import gzip
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
# Outside every git repository. Inside one, Claude Code treats each run as that repo's
# project: it loads the repo's .claude/ skills and settings, and hands the session the
# project's memory folder - where one benchmark run wrote notes into the user's real
# Claude memory for the connector repo.
RUNS = Path("/Users/cheng/agentaus_ab_runs")
PEER_DATA = Path("/Users/cheng/agentaus-bench/data")      # read-only; files are copied
def _newest_claude_code() -> str:
    """The binary the installed VS Code extension ships - the newest one, since VS Code
    updates the extension in place and leaves older versions behind."""
    import glob
    found = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")),
        key=lambda p: [int(x) for x in re.findall(r"claude-code-(\d+)\.(\d+)\.(\d+)", p)[0]])
    if not found:
        raise SystemExit("No Claude Code VS Code extension found under ~/.vscode/extensions")
    return found[-1]


CC = _newest_claude_code()
PY = "/Users/cheng/agentaus_connector_for_claude_code/.venv/bin/python"

ARMS = {
    "opus": ("claude-opus-5", 8795),
    "agentaus_base": ("agentaus", 8795),
    "agentaus_tuned": ("agentaus", 8796),
    "agentaus_next": ("agentaus", 8797),    # frozen snapshot in /Users/cheng/agentaus_ab_snap
    # Skills A/B: main as committed (a detached checkout in /Users/cheng/agentaus_ab_head)
    # against the working tree with the skill listing restored.
    "agentaus_head": ("agentaus", 8796),
    "agentaus_skills": ("agentaus", 8797),
}

NETWORK = re.compile(r"\b(curl|wget|urllib|requests\.get|httpx|huggingface|github\.com|"
                     r"datasets-server|paperswithcode)\b", re.I)


# --------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------

class Task:
    def __init__(self, suite, tid, prompt, setup, grade, *, web=False, max_turns=30,
                 timeout=1200):
        self.suite, self.id, self.prompt = suite, tid, prompt
        self.setup, self.grade = setup, grade
        self.web, self.max_turns, self.timeout = web, max_turns, timeout


def _he_problems():
    with gzip.open(DATA / "HumanEvalPlus.jsonl.gz", "rt") as fh:
        return [json.loads(line) for line in fh]


HE_PROMPT = """\
`solution.py` in this directory contains the signature and docstring of a Python \
function, `{entry}`, with no body.

Implement it so it is correct for every valid input the docstring describes - including \
edge cases such as empty inputs, zero, negatives, duplicates and single elements. Keep \
the function name, signature and any imports. Do not rename the file.

You may run Python to test your implementation. When you are done, `solution.py` must \
contain the complete, working function."""

HE_GRADER = r'''
import copy, json, math, sys
sys.setrecursionlimit(100000)
task = json.load(open(sys.argv[1]))
ns = {}
exec(compile(open("solution.py").read(), "solution.py", "exec"), ns)
cand = ns[task["entry_point"]]
ref_ns = {}
exec(task["prompt"] + task["canonical_solution"], ref_ns)
ref = ref_ns[task["entry_point"]]
out = {"base": False, "plus": False, "plus_failed_on": None}
try:
    tns = dict(ns)          # HumanEval's tests run in the solution's own namespace
    exec(task["test"], tns)
    tns["check"](cand)
    out["base"] = True
except BaseException as e:
    out["base_error"] = repr(e)[:200]
atol = task.get("atol") or 0

def same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(a, b, rel_tol=1e-6, abs_tol=max(atol, 1e-6)) or (a != a and b != b)
        except TypeError:
            return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b

ok = out["base"]
if ok:
    for inp in task["base_input"] + task["plus_input"]:
        try:
            want = ref(*copy.deepcopy(inp))
        except BaseException:
            continue            # the reference rejects it too: out of contract
        try:
            got = cand(*copy.deepcopy(inp))
        except BaseException as e:
            ok = False; out["plus_failed_on"] = [repr(inp)[:120], repr(e)[:120]]; break
        if not same(want, got):
            ok = False; out["plus_failed_on"] = [repr(inp)[:120], repr(got)[:80]]; break
out["plus"] = ok
print(json.dumps(out))
'''


def he_tasks(split):
    tasks = []
    for p in _he_problems():
        n = int(p["task_id"].split("/")[1])
        if split == "dev" and n % 2:
            continue
        if split == "test" and not n % 2:
            continue

        def setup(wd, p=p):
            (wd / "solution.py").write_text(p["prompt"])
            (wd / ".task.json").write_text(json.dumps(p))

        def grade(wd, transcript, p=p):
            (wd / ".grader.py").write_text(HE_GRADER)
            try:
                r = subprocess.run([sys.executable, ".grader.py", ".task.json"], cwd=wd,
                                   capture_output=True, text=True, timeout=180)
                g = json.loads(r.stdout.strip().splitlines()[-1])
            except Exception as e:
                g = {"base": False, "plus": False, "grader_error": repr(e)[:200]}
            (wd / ".grader.py").unlink(missing_ok=True)
            g["score"] = 1.0 if g.get("plus") else 0.0
            return g

        tasks.append(Task("he", p["task_id"].replace("/", "_"), HE_PROMPT.format(entry=p["entry_point"]),
                          setup, grade, max_turns=25, timeout=900))
    return tasks


MCQ_PROMPT = """\
`questions.json` in this directory holds {n} multiple-choice questions. Each has an \
`id`, a `question`, and lettered `options`.

Answer every question from your own knowledge and reasoning. You may run Python for \
arithmetic. Do not use the internet.

Write `answers.json` in this directory: a JSON object mapping each question's `id` to the \
single letter of the correct option, for example {{"q1": "C", "q2": "A"}}. Answer every \
question - if unsure, give your best guess."""

LETTERS = "ABCDEFGHIJ"


def _mcq_rows(suite):
    rows = json.load(open(DATA / f"{suite}.json"))
    out = []
    for i, r in enumerate(rows):
        if suite == "mmlu_pro":
            opts, ans, cat = r["options"], r["answer"], r["category"]
        else:
            opts, ans, cat = r["choices"], LETTERS[r["answer"]], r["subject"]
        out.append({"id": f"q{i}", "question": r["question"],
                    "options": {LETTERS[j]: o for j, o in enumerate(opts)},
                    "answer": ans, "category": cat})
    return out


def mcq_tasks(suite, split="all", batch=10):
    """Batches of `batch` questions. dev is the first half of the sample, test the
    second: tune on one, report on the other."""
    rows = _mcq_rows(suite)
    tasks = []
    half = len(rows) // 2
    for start in range(0, len(rows), batch):
        if split == "dev" and start >= half:
            continue
        if split == "test" and start < half:
            continue
        chunk = rows[start:start + batch]

        def setup(wd, chunk=chunk):
            (wd / "questions.json").write_text(json.dumps(
                [{k: q[k] for k in ("id", "question", "options")} for q in chunk], indent=1))

        def grade(wd, transcript, chunk=chunk):
            got = {}
            try:
                got = json.load(open(wd / "answers.json"))
            except Exception:
                pass
            detail = []
            for q in chunk:
                a = str(got.get(q["id"], "")).strip().upper()[:1]
                detail.append({"id": q["id"], "want": q["answer"], "got": a,
                               "category": q["category"], "ok": a == q["answer"]})
            right = sum(d["ok"] for d in detail)
            return {"score": right / len(chunk), "right": right, "n": len(chunk),
                    "answers_file": (wd / "answers.json").exists(), "detail": detail}

        tasks.append(Task(suite, f"{suite}_{start:03d}", MCQ_PROMPT.format(n=len(chunk)),
                          setup, grade, max_turns=12, timeout=900))
    return tasks


R1_PROMPT = """\
In this directory is `australia_macro.csv`, annual Australian macro data with columns: \
year, gdp_current_usd, gdp_growth_pct, rnd_pct_gdp (R&D expenditure as a percentage of \
GDP), gdp_per_capita_2015usd.

Using R (run it with `Rscript`), estimate an ordinary least squares regression of R&D \
intensity on income:

    rnd_pct_gdp = b0 + b1 * log(gdp_per_capita_2015usd) + e

Drop rows where either variable is missing. Save your code as `model.R` and run it. \
`model.R` must itself write `answer.json` here, with exactly these keys:

- "n": integer, observations in the regression
- "coef_log_gdppc": number, the estimated b1
- "se_log_gdppc": number, its standard error
- "r_squared": number
- "p_value": number, the p-value on b1
- "interpretation": a one-sentence string saying whether the relationship is \
statistically significant at the 5% level and what that means

The R packages `jsonlite` and `readxl` are installed. Report the real fitted values."""

R2_PROMPT = """\
Using R, and data you download yourself:

1. Find, on data.gov.au, the Australian Taxation Office dataset that publishes the \
Report of information about Research and Development Tax Incentive entities, and \
download the resource for the 2022-23 income year into this directory. (data.gov.au \
has a CKAN API at https://data.gov.au/data/api/3/action/ if you want it.)
2. Take the column "Total R&D expenditure (notional deductions less feedstock \
adjustments) $", keep strictly positive values, and select the 500 largest.
3. Estimate the Pareto tail exponent of that top-500 distribution with the \
Gabaix-Ibragimov rank-size regression: OLS of log(rank - 0.5) on log(expenditure). The \
exponent alpha is minus the slope, and its standard error is alpha * sqrt(2 / n).

Save your R code as `pareto.R` and run it with `Rscript`. `pareto.R` must itself read the \
downloaded file and write `answer.json` here with exactly these keys:

- "resource_url": the URL you downloaded the file from
- "n_positive": integer, how many strictly positive values the column has
- "alpha": number
- "se_alpha": number, the Gabaix-Ibragimov standard error
- "r_squared": number, of the rank-size regression
- "xmin": number, the smallest expenditure among the 500

The R packages `readxl` and `jsonlite` are installed."""


def _close(got, want, tol):
    try:
        g = float(str(got).replace(",", ""))
    except (TypeError, ValueError):
        return False
    return abs(g - want) <= tol * max(abs(want), 1e-12)


def _rerun_r(wd, script, timeout=300):
    """Re-run the agent's R script in a clean copy: an answer.json the script does not
    reproduce was typed in by hand, however right its numbers look."""
    clone = wd.parent / (wd.name + ".rerun")
    shutil.rmtree(clone, ignore_errors=True)
    shutil.copytree(wd, clone, ignore=shutil.ignore_patterns("answer.json", ".claude*"))
    try:
        subprocess.run(["Rscript", script], cwd=clone, capture_output=True, text=True,
                       timeout=timeout)
        return json.load(open(clone / "answer.json"))
    except Exception:
        return None
    finally:
        shutil.rmtree(clone, ignore_errors=True)


def econ_tasks():
    ols = json.load(open(PEER_DATA / "ground_truth_ols.json"))
    pareto = {"n_positive": 13131, "alpha": 1.49722067017857, "se_alpha": 0.09469254955296041,
              "r_squared": 0.9848867679700225, "xmin": 5445594}

    def r1_setup(wd):
        shutil.copy(PEER_DATA / "australia_macro.csv", wd)

    def r1_grade(wd, transcript):
        checks = {}
        try:
            a = json.load(open(wd / "answer.json"))
        except Exception:
            return {"score": 0.0, "checks": {"answer.json": False}}
        for k, tol in [("n", 0.0001), ("coef_log_gdppc", 0.02), ("se_log_gdppc", 0.02),
                       ("r_squared", 0.05), ("p_value", 0.05)]:
            checks[k] = _close(a.get(k), ols[k], tol)
        it = str(a.get("interpretation", "")).lower()
        checks["says_not_significant"] = any(w in it for w in (
            "not statistic", "not signif", "insignificant", "cannot reject", "fail to reject",
            "fails to reject", "no statistic", "non-significant"))
        checks["model.R_exists"] = (wd / "model.R").exists()
        again = _rerun_r(wd, "model.R") if checks["model.R_exists"] else None
        checks["model.R_reproduces"] = bool(again) and _close(
            again.get("coef_log_gdppc"), ols["coef_log_gdppc"], 0.02)
        return {"score": sum(checks.values()) / len(checks), "checks": checks, "answer": a}

    def r2_grade(wd, transcript):
        checks = {}
        try:
            a = json.load(open(wd / "answer.json"))
        except Exception:
            return {"score": 0.0, "checks": {"answer.json": False}}
        url = str(a.get("resource_url", ""))
        checks["resource_is_2022_23"] = "e9c059c8-a801-4068-844e-9d4ee84a1d81" in url
        checks["n_positive"] = _close(a.get("n_positive"), pareto["n_positive"], 0.0001)
        for k, tol in [("alpha", 0.01), ("se_alpha", 0.02), ("r_squared", 0.005),
                       ("xmin", 0.0001)]:
            checks[k] = _close(a.get(k), pareto[k], tol)
        checks["downloaded_a_file"] = any(p.suffix.lower() in (".xlsx", ".xls", ".csv")
                                          for p in wd.iterdir())
        checks["pareto.R_exists"] = (wd / "pareto.R").exists()
        again = _rerun_r(wd, "pareto.R") if checks["pareto.R_exists"] else None
        checks["pareto.R_reproduces"] = bool(again) and _close(again.get("alpha"),
                                                               pareto["alpha"], 0.01)
        return {"score": sum(checks.values()) / len(checks), "checks": checks, "answer": a}

    return [Task("econ_r", "R1_ols", R1_PROMPT, r1_setup, r1_grade, max_turns=30, timeout=1500),
            Task("econ_r", "R2_pareto", R2_PROMPT, lambda wd: None, r2_grade, web=True,
                 max_turns=40, timeout=2400)]


def tasks_for(suite, split):
    if suite == "he":
        return he_tasks(split)
    if suite in ("mmlu", "mmlu_pro"):
        return mcq_tasks(suite, split)
    if suite == "econ_r":
        return econ_tasks()
    raise SystemExit(f"unknown suite {suite}")


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------

def clean_env(port):
    """The environment the VS Code extension gives its Claude Code process - and nothing
    inherited from whatever launched this harness.

    Launched from another Claude Code session, every run otherwise inherited that
    session's identity: CLAUDE_CODE_ENTRYPOINT=claude-desktop (desktop-only behaviour),
    its messaging socket and session ids (runs registered as peers of the launching
    session), and CLAUDE_EFFORT=xhigh (Opus at maximum effort, not the VS Code default).
    """
    keep = ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "agentaus"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


# Bridges with Agentaus web search on, for the one task where finding data is the point.
WEB_PORT = {8795: 8793, 8796: 8794, 8797: 8798}


INFRA = re.compile(r"^API Error|Connection refused|ECONNREFUSED|overloaded_error|rate_limit|"
                   r"spend limit|usage limit|session limit|hit your .* limit", re.I)


def run_one(arm, task, label, attempts=3):
    """One run, retried when it failed for a reason that is not the model's: a bridge
    that was down scored every run through it zero, which measures nothing."""
    for attempt in range(attempts):
        rec = _run_once(arm, task, label)
        if not rec.get("infra_error"):
            return rec
        time.sleep(15 * (attempt + 1))
    return rec


def _run_once(arm, task, label):
    model, port = ARMS[arm]
    if task.web:
        port = WEB_PORT[port]
    wd = RUNS / label / arm / task.id
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    task.setup(wd)

    env = clean_env(port)
    settings = json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                                   "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus"},
                           "autoMemoryEnabled": False})
    cmd = [CC, "-p", "--model", model, "--settings", settings,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions", "--max-turns", str(task.max_turns)]
    if not task.web:
        cmd += ["--disallowedTools", "WebSearch", "WebFetch"]
    # The prompt goes on stdin: --disallowedTools is variadic and swallows a trailing
    # positional argument as one more tool name.

    started = time.time()
    timed_out = False
    with open(wd.parent / f"{task.id}.transcript.jsonl", "w") as out:
        try:
            subprocess.run(cmd, cwd=wd, env=env, input=task.prompt, text=True, stdout=out,
                           stderr=subprocess.DEVNULL, timeout=task.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    wall = time.time() - started

    lines = (wd.parent / f"{task.id}.transcript.jsonl").read_text().splitlines()
    result, tool_cmds, tool_inputs = {}, [], []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            result = ev
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    tool_cmds.append(json.dumps(block.get("input"))[:400])
                    tool_inputs.append(json.dumps(block.get("input")))
    network = [c for c in tool_cmds if NETWORK.search(c)]
    # Anything a run touched outside its own directory. Expected to be empty; a
    # benchmark that writes to the user's real files is broken however well it scores.
    escaped = sorted({m for c in tool_inputs for m in re.findall(r"(/Users/[^\s\"'\\]+)", c)
                      if not m.startswith(str(wd)) and not m.startswith(str(PEER_DATA))
                      and "/Library/R/" not in m})
    grade = task.grade(wd, lines)
    rec = {"arm": arm, "suite": task.suite, "task": task.id, "label": label,
           "wall_s": round(wall, 1), "timed_out": timed_out,
           "num_turns": result.get("num_turns"), "cost_usd": result.get("total_cost_usd"),
           "is_error": result.get("is_error"), "usage": result.get("usage"),
           "result_text": (result.get("result") or "")[:1500],
           "tool_calls": len(tool_cmds), "network_commands": network if not task.web else [],
           "escaped_paths": escaped,
           "infra_error": bool(INFRA.search((result.get("result") or "")[:300]))
                          and not tool_cmds,
           "grade": grade}
    (wd.parent / f"{task.id}.json").write_text(json.dumps(rec, indent=1))
    return rec


def cmd_run(args):
    tasks = tasks_for(args.suite, args.split)
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t.id in wanted]
    if args.limit:
        rng = random.Random(7)
        tasks = sorted(rng.sample(tasks, min(args.limit, len(tasks))), key=lambda t: t.id)
    arms = args.arms.split(",")
    todo = []
    for arm in arms:
        for t in tasks:
            done = RUNS / args.label / arm / f"{t.id}.json"
            if args.resume and done.exists():
                continue
            todo.append((arm, t))
    print(f"{len(todo)} runs: {len(tasks)} task(s) x {arms}", flush=True)
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_one, arm, t, args.label): (arm, t) for arm, t in todo}
        for f in cf.as_completed(futures):
            arm, t = futures[f]
            try:
                r = f.result()
                g = r["grade"]
                extra = "" if "right" not in g else f" {g['right']}/{g['n']}"
                print(f"[{args.label}] {arm:15s} {t.id:18s} score={g['score']:.2f}{extra} "
                      f"{r['wall_s']:6.1f}s turns={r['num_turns']} "
                      f"{'TIMEOUT ' if r['timed_out'] else ''}"
                      f"{'NET! ' if r['network_commands'] else ''}"
                      f"{'ESCAPED! ' if r.get('escaped_paths') else ''}", flush=True)
            except Exception as e:
                print(f"[{args.label}] {arm} {t.id} CRASHED {e!r}", flush=True)


def load(label):
    recs = []
    for p in (RUNS / label).glob("*/*.json"):
        recs.append(json.loads(p.read_text()))
    return recs


def cmd_report(args):
    recs = load(args.label)
    infra = [r for r in recs if r.get("infra_error")]
    if infra:
        print(f"EXCLUDED {len(infra)} run(s) that failed on infrastructure, not the model")
    recs = [r for r in recs if not r.get("infra_error")]
    by = {}
    for r in recs:
        by.setdefault((r["suite"], r["arm"]), []).append(r)
    print(f"label {args.label}: {len(recs)} runs")
    print(f"{'suite':9s} {'arm':15s} {'n':>4s} {'score':>7s} {'base':>6s} {'wall p50':>9s} "
          f"{'turns':>6s} {'timeouts':>8s} {'net':>4s}")
    for (suite, arm), rs in sorted(by.items()):
        if suite in ("mmlu", "mmlu_pro"):
            right = sum(r["grade"]["right"] for r in rs); n = sum(r["grade"]["n"] for r in rs)
            score = right / n if n else 0; count = n
        else:
            score = sum(r["grade"]["score"] for r in rs) / len(rs); count = len(rs)
        base = ""
        if suite == "he":
            base = f"{sum(bool(r['grade'].get('base')) for r in rs) / len(rs):.3f}"
        walls = sorted(r["wall_s"] for r in rs)
        turns = [r["num_turns"] or 0 for r in rs]
        print(f"{suite:9s} {arm:15s} {count:4d} {score:7.3f} {base:>6s} "
              f"{walls[len(walls) // 2]:8.1f}s {sum(turns) / len(turns):6.1f} "
              f"{sum(r['timed_out'] for r in rs):8d} {sum(bool(r['network_commands']) for r in rs):4d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--suite", required=True)
    r.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    r.add_argument("--arms", default="agentaus_tuned")
    r.add_argument("--label", required=True)
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--only", default="")
    r.add_argument("--resume", action="store_true")
    r.set_defaults(func=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("--label", required=True)
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

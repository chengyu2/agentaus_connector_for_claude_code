#!/usr/bin/env python3
"""Ultracode benchmark: Claude Code driving a multi-agent `Workflow`, Agentaus against Opus.

Every run is one headless session of the Claude Code binary the VS Code extension ships,
in a fresh directory outside any repository, graded afterwards with no model in the loop.

    ./ultra.py setup                                   # build fixtures from local sources
    ./ultra.py run --arms agentaus,opus --tasks rdti,c1913,tender --reps 2 --label u1
    ./ultra.py report --label u1

Tasks - each the kind of work ultracode is for:
    rdti    data analysis across four spreadsheets of the public ATO R&D Tax Incentive
            report: ABNs written three ways, an amendment file that overrides (one
            amendment is $0), two entities with no ABN. Graded on eight numbers
            computed independently by build_rdti.py.
    c1913   five bugs injected into the 1913_collective analysis pipeline (one not among
            the reported symptoms) plus a `--json` feature. Graded by comparing the fixed
            module's behaviour with the untouched original.
    tender  answer an RFQ from a past proposal and README, citing sources, with two
            questions the evidence cannot answer. Graded on required facts and on
            refusing to invent insurance or client records.

Variants:
    ui        the prompt carries the system reminder the VS Code extension adds when the
              user types "ultracode" - the session a user actually has. Default.
    headless  the prompt as typed. Headless Claude Code adds no reminder, so whether to
              orchestrate is the model's own call.

The 1913_collective sources hold personal information. Nothing from them is written into
this repository: fixtures are built under --runs from the user's local checkout.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE / "tasks"
HOME = Path.home()
RUNS = Path(os.environ.get("ULTRA_RUNS", HOME / "agentaus_ultra_runs"))
FIXTURES = RUNS / "_fixtures"
SOURCE_1913 = Path(os.environ.get("ULTRA_1913", HOME / "PycharmProjects" / "1913_collective"))
SOURCE_ATO = Path(os.environ.get("ULTRA_ATO", HOME / "Downloads" / (
    "report-of-information-about-research-and-development-tax-incentive-entities.xlsx")))
PROPOSAL = "ML Research Labs - B and C Family Trust Proposal - Strategic R&D Management Solution.docx"

ARMS = {"agentaus": "agentaus", "opus": "claude-opus-5-5"}

# A run that ended for a reason that is not the model's: excluded from scores, not zeroed.
# A context-length 400 is the bridge's failure to fit the conversation - the thing under
# test - so it counts against the run; only limits and refused connections are excluded.
INFRA = re.compile(r"^API Error: (?:5\d\d|529)|Connection refused|ECONNREFUSED|overloaded_error|"
                   r"rate_limit|spend limit|usage limit|session limit|hit your .* limit", re.I)

# The five injected bugs: (original, broken). The README and docstrings describe the
# original behaviour, so each is findable without seeing this list.
BUGS_1913 = [
    ("if cur is None or p['depth'] > cur['depth']:",
     "if cur is None or p['depth'] < cur['depth']:"),
    ("SPELLING = {'Individualisation': 'Individualization', 'Maximiser': 'Maximizer'}",
     "SPELLING = {'Individualisation': 'Individualization', 'Maximiser': 'Maximiser'}"),
    ("return max(counts, key=lambda d: (counts[d], -best[d]))",
     "return max(counts, key=lambda d: (counts[d], best[d]))"),
    ("clash = [n for n in BANK if n.split()[0] == first and n != name]",
     "clash = [n for n in BANK if n.split()[0] == first]"),
    ("return sum(1 for p in people if (rank(p, theme) or 99) <= k)",
     "return sum(1 for p in people if (rank(p, theme) or 99) < k)"),
]


def _newest_claude_code() -> str:
    found = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")),
        key=lambda p: [int(x) for x in re.findall(r"claude-code-(\d+)\.(\d+)\.(\d+)", p)[0]])
    if not found:
        raise SystemExit("No Claude Code VS Code extension found under ~/.vscode/extensions")
    return found[-1]


def _python() -> Path:
    """A Python with pandas, openpyxl, pypdf and python-docx, first on every run's PATH so
    both arms get the same tools."""
    return FIXTURES / "venv" / "bin" / "python"


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------

def cmd_setup(args):
    FIXTURES.mkdir(parents=True, exist_ok=True)
    if not _python().exists():
        subprocess.run([sys.executable, "-m", "venv", str(FIXTURES / "venv")], check=True)
        subprocess.run([str(_python()), "-m", "pip", "-q", "install", "pandas", "openpyxl",
                        "pypdf", "python-docx"], check=True)
    # rdti
    out = FIXTURES / "rdti"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    subprocess.run([str(_python()), str(HERE / "build_rdti.py"), str(SOURCE_ATO), str(out)],
                   check=True, stdout=subprocess.DEVNULL)
    # c1913: the pipeline and its profile bank, with the bugs injected
    out = FIXTURES / "c1913"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    shutil.copytree(SOURCE_1913 / "tools", out / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(SOURCE_1913 / "strength_reports", out / "strength_reports")
    shutil.copy(SOURCE_1913 / "README.md", out / "README.md")
    bank = (out / "tools" / "bank.py").read_text()
    shutil.copy(out / "tools" / "bank.py", FIXTURES / "bank_original.py")
    for good, bad in BUGS_1913:
        if bank.count(good) != 1:
            raise SystemExit(f"1913 source changed; cannot inject: {good}")
        bank = bank.replace(good, bad)
    (out / "tools" / "bank.py").write_text(bank)
    # tender
    out = FIXTURES / "tender"
    shutil.rmtree(out, ignore_errors=True)
    (out / "past").mkdir(parents=True)
    shutil.copy(SOURCE_1913 / PROPOSAL, out / "past" / PROPOSAL)
    shutil.copy(SOURCE_1913 / "README.md", out / "past" / "1913_README.md")
    shutil.copy(TASKS / "tender_RFQ.md", out / "RFQ.md")
    print(f"fixtures ready in {FIXTURES}")


# --------------------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------------------

def grade(task: str, wd: Path) -> dict:
    py = str(_python())
    if task == "rdti":
        key = json.loads((FIXTURES / "rdti_answer_key.json").read_text())
        try:
            got = json.loads((wd / "answers.json").read_text())
        except (OSError, ValueError) as exc:
            return {"score": 0.0, "error": repr(exc)[:120]}
        right = [k for k, v in key.items() if got.get(k) == v]
        return {"score": round(len(right) / len(key), 3), "wrong": [k for k in key if k not in right]}
    if task == "c1913":
        r = subprocess.run([py, str(HERE / "grade_1913.py"), str(wd), str(FIXTURES / "bank_original.py")],
                           capture_output=True, text=True, timeout=600)
        shutil.rmtree(wd / ".orig", ignore_errors=True)
    else:
        r = subprocess.run([py, str(HERE / "grade_tender.py"), str(wd)],
                           capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"score": 0.0, "grader_error": (r.stderr or "")[-300:]}


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------

def clean_env(port: int) -> dict:
    """What the VS Code extension gives its Claude Code process, and nothing inherited."""
    keep = ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = f"{_python().parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "agentaus"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    # Headless Claude Code stops waiting for background work after a ceiling and kills
    # it - measured, a workflow ended ten minutes in with "[Request interrupted by user]"
    # and its session scored zero. An open VS Code session never does that, so neither
    # does this one; the run's own timeout still bounds it.
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    return env


def prompt_for(task: str, variant: str) -> str:
    text = (TASKS / f"{task}.txt").read_text()
    if variant == "ui":
        text += (TASKS / "ultracode_reminder.txt").read_text()
    return text


def transcript_stats(lines: list) -> dict:
    """What the session did with `Workflow`: launches, rejected scripts, duplicates."""
    calls, errors, by_path, results = {}, 0, 0, None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use" and b.get("name") == "Workflow":
                    calls[b["id"]] = b.get("input") or {}
                    by_path += 1 if (b.get("input") or {}).get("scriptPath") else 0
        if ev.get("type") == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("tool_use_id") in calls and b.get("is_error"):
                    errors += 1
        if ev.get("type") == "result":
            results = ev
    return {"workflow_calls": len(calls), "workflow_rejected": errors,
            "workflow_by_path": by_path,
            "num_turns": (results or {}).get("num_turns"),
            "result_text": ((results or {}).get("result") or "")[:800]}


def run_one(arm: str, task: str, variant: str, rep: int, label: str, port: int,
            timeout: int) -> dict:
    wd = RUNS / label / f"{arm}-{task}-{variant}-{rep}"
    shutil.rmtree(wd, ignore_errors=True)
    shutil.copytree(FIXTURES / task, wd)
    settings = json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                                   "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus"},
                           "autoMemoryEnabled": False})
    cmd = [_newest_claude_code(), "-p", "--model", ARMS[arm], "--settings", settings,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions", "--max-turns", "80",
           "--disallowedTools", "WebSearch", "WebFetch"]
    transcript = wd.with_suffix(".jsonl")
    started, timed_out = time.time(), False
    with open(transcript, "w") as out:
        try:
            subprocess.run(cmd, cwd=wd, env=clean_env(port), input=prompt_for(task, variant),
                           text=True, stdout=out, stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    rec = {"arm": arm, "task": task, "variant": variant, "rep": rep, "label": label,
           "wall_s": round(time.time() - started, 1), "timed_out": timed_out,
           **transcript_stats(transcript.read_text().splitlines()), "grade": grade(task, wd)}
    # A limit can land mid-run, so it is read from the transcript's last words, not the wall.
    tail = rec["result_text"][:300] or transcript.read_text()
    rec["infra_error"] = bool(INFRA.search(tail))
    wd.with_suffix(".json").write_text(json.dumps(rec, indent=1))
    return rec


def cmd_run(args):
    if not (FIXTURES / "rdti_answer_key.json").exists():
        raise SystemExit("run `ultra.py setup` first")
    todo = [(a, t, v, r) for r in range(1, args.reps + 1) for a in args.arms.split(",")
            for t in args.tasks.split(",") for v in args.variants.split(",")
            if not (args.resume and (RUNS / args.label / f"{a}-{t}-{v}-{r}.json").exists())]
    print(f"{len(todo)} runs", flush=True)
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_one, a, t, v, r, args.label, args.port, args.timeout): (a, t, v, r)
                   for a, t, v, r in todo}
        for f in cf.as_completed(futures):
            a, t, v, r = futures[f]
            try:
                rec = f.result()
                print(f"[{args.label}] {a:9s} {t:7s} {v:8s} #{r} score={rec['grade'].get('score', 0):.2f} "
                      f"{rec['wall_s']:6.0f}s workflow={rec['workflow_calls']} "
                      f"rejected={rec['workflow_rejected']}{' TIMEOUT' if rec['timed_out'] else ''}"
                      f"{' INFRA' if rec.get('infra_error') else ''}",
                      flush=True)
            except Exception as exc:
                print(f"[{args.label}] {a} {t} {v} #{r} CRASHED {exc!r}", flush=True)


def cmd_report(args):
    # Several labels pool their runs: one round of two is too few to tell a change from
    # noise on tasks whose scores ranged 0.38-0.88 between rounds.
    recs = [json.loads(p.read_text()) for label in args.label.split(",")
            for p in (RUNS / label).glob("*.json")]
    infra = [r for r in recs if r.get("infra_error")]
    if infra:
        print(f"EXCLUDED {len(infra)} run(s) that failed on infrastructure, not the model")
    recs = [r for r in recs if not r.get("infra_error")]
    by: dict = {}
    for r in recs:
        by.setdefault((r["task"], r["variant"], r["arm"]), []).append(r)
    print(f"{'task':7s} {'variant':8s} {'arm':9s} {'n':>2s} {'score':>6s} {'min-max':>9s} "
          f"{'wall p50':>9s} {'used wf':>7s} {'rejected':>8s}")
    for (task, variant, arm), rs in sorted(by.items()):
        walls = sorted(r["wall_s"] for r in rs)
        scores = [r["grade"].get("score", 0) for r in rs]
        print(f"{task:7s} {variant:8s} {arm:9s} {len(rs):2d} "
              f"{sum(scores) / len(scores):6.3f} {min(scores):4.2f}-{max(scores):4.2f} "
              f"{walls[len(walls) // 2]:8.0f}s {sum(r['workflow_calls'] > 0 for r in rs):4d}/{len(rs)} "
              f"{sum(r['workflow_rejected'] for r in rs):8d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(func=cmd_setup)
    r = sub.add_parser("run")
    r.add_argument("--arms", default="agentaus,opus")
    r.add_argument("--tasks", default="rdti,c1913,tender")
    r.add_argument("--variants", default="ui")
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--label", required=True)
    r.add_argument("--port", type=int, default=8787)
    r.add_argument("--jobs", type=int, default=3)
    r.add_argument("--timeout", type=int, default=2700)
    r.add_argument("--resume", action="store_true")
    r.set_defaults(func=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("--label", required=True)
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

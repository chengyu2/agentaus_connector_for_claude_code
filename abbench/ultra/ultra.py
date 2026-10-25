#!/usr/bin/env python3
"""Ultracode benchmark: Claude Code driving a multi-agent `Workflow`, Agentaus against Opus.

Every run is one headless session of the Claude Code binary the VS Code extension ships,
in a fresh directory outside any repository, graded afterwards with no model in the loop.

    ./ultra.py setup                                   # build fixtures from local sources
    ./ultra.py run --arms agentaus,opus --tasks rdti,c1913,tender --reps 2 --label u1
    ./ultra.py report --label u1

For benchmark rounds use loop.py: it gates launches on upstream health, voids and reruns
runs an outage spoiled, and keeps a ledger tied to the code the bridge loaded.

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
import signal
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
HE_SOURCE = HERE.parent.parent / "benchmarks" / "data" / "HumanEval.jsonl.gz"
PROPOSAL = "ML Research Labs - B and C Family Trust Proposal - Strategic R&D Management Solution.docx"

ARMS = {"agentaus": "agentaus", "opus": "claude-opus-5-5"}

# A run that ended for a reason that is not the model's: excluded from scores, not zeroed.
# A context-length 400 is the bridge's failure to fit the conversation - the thing under
# test - so it counts against the run; only limits and refused connections are excluded.
# A spent Agentaus quota (402) is infrastructure too. Measured (d16): two runs ended on
# "API Error: 402 ... exceeded your current quota" and were scored 0.0 and 0.375 as valid.
INFRA = re.compile(r"^API Error: (?:402|5\d\d|529)|Connection refused|ECONNREFUSED|overloaded_error|"
                   r"rate_limit|spend limit|usage limit|session limit|hit your .* limit|exceeded your current quota", re.I)

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


# `c1913h`: the same five plus four more, none of them among the reported symptoms - the
# kind Agentaus rarely found, and where a frontier model has room to miss one too.
BUGS_1913H = BUGS_1913 + [
    ("n = re.sub(r'\\s+', ' ', n).strip().title()",
     "n = re.sub(r'\\s+', ' ', n).strip().capitalize()"),
    ("    if depth < 5:\n        return None", "    if depth < 6:\n        return None"),
    ('for t in "Achiever Arranger Belief Consistency Deliberative Discipline Focus Responsibility Restorative".split():',
     'for t in "Achiever Analytical Arranger Belief Consistency Deliberative Discipline Focus Responsibility Restorative".split():'),
    ('for t in "Analytical Context Futuristic Ideation Input Intellection Learner Strategic".split():',
     'for t in "Context Futuristic Ideation Input Intellection Learner Strategic".split():'),
    ("return statistics.mean(rank(p, theme) for p in people)",
     "return statistics.median(rank(p, theme) for p in people)"),
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
    # c1913 / c1913h: the pipeline and its profile bank, with the bugs injected
    for name, bugs in (("c1913", BUGS_1913), ("c1913h", BUGS_1913H)):
        out = FIXTURES / name
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir()
        shutil.copytree(SOURCE_1913 / "tools", out / "tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(SOURCE_1913 / "strength_reports", out / "strength_reports")
        shutil.copy(SOURCE_1913 / "README.md", out / "README.md")
        bank = (out / "tools" / "bank.py").read_text()
        shutil.copy(out / "tools" / "bank.py", FIXTURES / "bank_original.py")
        for good, bad in bugs:
            if bank.count(good) != 1:
                raise SystemExit(f"1913 source changed; cannot inject: {good}")
            bank = bank.replace(good, bad)
        (out / "tools" / "bank.py").write_text(bank)
    # he10: ten HumanEval functions, graded with edge inputs too
    out = FIXTURES / "he10"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    subprocess.run([str(_python()), str(HERE / "build_he.py"), str(HE_SOURCE), str(out)],
                   check=True, stdout=subprocess.DEVNULL)
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

def grade(task: str, wd: Path, graders_dir: Path = HERE) -> dict:
    """`graders_dir` lets `loop.py regrade` compare against an older copy of the graders."""
    py = str(_python())
    if task == "rdti":
        key = json.loads((FIXTURES / "rdti_answer_key.json").read_text())
        try:
            got = json.loads((wd / "answers.json").read_text())
        except (OSError, ValueError) as exc:
            return {"score": 0.0, "error": repr(exc)[:120]}
        right = [k for k, v in key.items() if got.get(k) == v]
        return {"score": round(len(right) / len(key), 3), "wrong": [k for k in key if k not in right]}
    if task in ("c1913", "c1913h"):
        r = subprocess.run([py, str(Path(graders_dir) / "grade_1913.py"), str(wd), str(FIXTURES / "bank_original.py")]
                           + (["--hard"] if task == "c1913h" else []),
                           capture_output=True, text=True, timeout=600)
        shutil.rmtree(wd / ".orig", ignore_errors=True)
    elif task == "he10":
        r = subprocess.run([py, str(Path(graders_dir) / "grade_he.py"), str(wd), str(FIXTURES / "he10_key.json")],
                           capture_output=True, text=True, timeout=900)
    else:
        r = subprocess.run([py, str(Path(graders_dir) / "grade_tender.py"), str(wd)],
                           capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"score": 0.0, "grader_error": (r.stderr or "")[-300:]}


def safe_grade(task: str, wd: Path) -> dict:
    """`grade`, but a grader crash or timeout is recorded instead of losing the run: the
    record is still written, with score None so it is reported as ungraded, not as 0."""
    try:
        return grade(task, wd)
    except BaseException as exc:  # noqa: BLE001 - TimeoutExpired, KeyboardInterrupt in a worker
        if isinstance(exc, KeyboardInterrupt):
            raise
        return {"score": None, "grader_error": repr(exc)[:300]}


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
    # A graph turn can wait for minutes on Agentaus with only keepalive pings; measured
    # (cc_probe mock_idle1800) a 650 s streamed turn completes with this and falls back to a
    # non-streaming re-send without it.
    env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] = "1800000"
    # Measured (d8, d11): one stalled request was re-sent non-streaming 9 times, and a run
    # launched into an outage died on its first request after 62 min. Measured against a mock
    # whose non-streaming reply never comes (API_TIMEOUT_MS=20000): 10 sends over 320 s by
    # default, 2 sends with this. One retry rides out a blip; the bridge retries the rest.
    env["CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES"] = "1"
    return env


def kill_session(proc: subprocess.Popen, wd: Path | None = None) -> None:
    """Kill the run's whole process group, then anything still working inside its directory:
    Bash-tool children outlive a kill of the parent and keep writing while the run is graded,
    and a child that called setsid has left the group."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # The leader may exit on SIGTERM while a member of its group ignores it.
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if wd is not None:
        for pid in pids_in(wd):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def pids_in(wd: Path) -> list[int]:
    """Processes whose working directory is `wd` or below it (lsof's cwd records; `+D` would
    walk every file in the tree)."""
    root = str(Path(wd).resolve())
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-F", "pn"], capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids, pid = [], None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid not in (None, os.getpid()):
            path = line[1:]
            if path == root or path.startswith(root + os.sep):
                pids.append(pid)
    return sorted(set(pids))


def launch(cmd: list, wd: Path, env: dict, prompt: str, transcript: Path) -> subprocess.Popen:
    """The session as its own process group, so a timeout or abort can kill all of it."""
    out = open(transcript, "w")
    try:
        proc = subprocess.Popen(cmd, cwd=wd, env=env, stdin=subprocess.PIPE, stdout=out,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    finally:
        out.close()  # the child holds its own copy
    proc.stdin.write(prompt)
    proc.stdin.close()
    return proc


def session_cmd(arm: str, port: int) -> list:
    settings = json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                                   "ANTHROPIC_CUSTOM_MODEL_OPTION": "agentaus"},
                           "autoMemoryEnabled": False})
    return [_newest_claude_code(), "-p", "--model", ARMS[arm], "--settings", settings,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions", "--max-turns", "80",
            "--disallowedTools", "WebSearch", "WebFetch"]


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
    transcript = wd.with_suffix(".jsonl")
    started, timed_out = time.time(), False
    proc = launch(session_cmd(arm, port), wd, clean_env(port), prompt_for(task, variant), transcript)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Also on a clean exit: background Bash children can outlive the session.
        kill_session(proc, wd)
    ended = time.time()
    rec = {"arm": arm, "task": task, "variant": variant, "rep": rep, "label": label,
           "started_at": round(started, 1), "ended_at": round(ended, 1),
           "wall_s": round(ended - started, 1), "timed_out": timed_out,
           **transcript_stats(transcript.read_text(errors="replace").splitlines()),
           "grade": safe_grade(task, wd)}
    # A limit can land mid-run, so it is read from the transcript's last words, not the wall.
    # Only the session's own last words: the whole transcript quotes tool docs and
    # results that mention limits, which flagged five timed-out runs as infrastructure.
    tail = rec["result_text"][:300] or "\n".join(transcript.read_text(errors="replace").splitlines()[-3:])
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
                score = rec["grade"].get("score")
                print(f"[{args.label}] {a:9s} {t:7s} {v:8s} #{r} "
                      f"score={'ungraded' if score is None else format(score, '.2f')} "
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
    ungraded = [r for r in recs if r["grade"].get("score") is None]
    if ungraded:
        print(f"EXCLUDED {len(ungraded)} run(s) whose grader failed (regrade them: loop.py regrade)")
    recs = [r for r in recs if r["grade"].get("score") is not None]
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
    r.add_argument("--timeout", type=int, default=7200)
    r.add_argument("--resume", action="store_true")
    r.set_defaults(func=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("--label", required=True)
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

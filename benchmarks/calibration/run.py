"""Calibration study: baseline vs prompt variants, on HLE-Diamond and live agentic tasks.

    python -m benchmarks.calibration.run selfcheck
    python -m benchmarks.calibration.run hle --parquet PATH --n 20 [--conditions a,b]
                                             [--repeat 1 | --repeat baseline=2]
    python -m benchmarks.calibration.run live [--tasks id,id] [--conditions a,b] [--repeat 1]
    python -m benchmarks.calibration.run judge-export --parquet PATH --n 20

Raw results go to benchmarks/calibration/results/raw (gitignored). Set AGENTAUS_API_KEY,
and CALIBRATION_USAGE_LOG to append every call's tokens and AUD cost to a usage log.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import agent, hle, prompts, tasks
from .sandbox import Sandbox

RAW = Path(os.environ.get("CALIBRATION_RAW") or Path(__file__).with_name("results") / "raw")

# What a reference solver prints for each tools task, run inside the task's sandbox,
# and a good and a bad answer for every graded task.
REFERENCE_SOLVERS = {
    "tools-logcount": "python -c \"import re;print(sum(1 for l in open('app.log') if ' ERROR ' in l "
                      "and '[billing]' in l and '09:00:00'<=l[11:19]<'17:00:00'))\"",
    "tools-revenue": "python -c \"import csv;print(round(sum(int(r['qty'])*float(r['unit_price']) "
                     "for r in csv.DictReader(open('sales.csv')) if r['region']=='APAC' and "
                     "r['date'][5:7] in ('07','08','09') and r['status']!='refunded'),2))\"",
    "tools-join": "python -c \"import csv,collections;t={r['customer_id']:r['tier'] for r in "
                  "csv.DictReader(open('customers.csv'))};c=collections.Counter(r['customer_ref']"
                  ".strip().upper() for r in csv.DictReader(open('orders.csv')));"
                  "print(sum(1 for k,v in t.items() if v=='Gold' and c[k]>=3))\"",
}
GOOD_BAD = {
    "tools-config": ("600 seconds", "900"),
    "tools-image": ("It cannot be determined: the figure is only in the image", "1,250 signups"),
    "tools-absent": ("v3.0 has not been deployed to production", "2026-06-02"),
    "web-email": ("lostproperty@harbourline.example", "hello@harbourline.example"),
    "web-fare": ("$21.50", "$19.50"),
    "web-count": ("2 disruptions on route F2", "3 disruptions on route F2"),
    "web-lastferry": ("22:50", "23:40"),
    "web-hours": ("Cannot be determined from the page; the hours are in an image",
                  "10:00 to 16:00"),
    "web-wharf": ("I cannot tell: the status is loaded by a script and not shown",
                  "Yes, Manly wharf is open"),
}


def selfcheck() -> int:
    failures = 0
    for task in tasks.all_tasks():
        if task.kind == "coding":
            for label, files, want in (("starter", task.files, False),
                                       ("reference", {**task.files, **tasks.REFERENCE[task.id]}, True)):
                box = Sandbox(files)
                try:
                    box.add_hidden(task.hidden)
                    out = box.bash("python -m unittest -q tests.test_hidden 2>&1 | tail -3")
                finally:
                    box.close()
                passed = out.rstrip().endswith("OK")
                ok = passed == want
                failures += not ok
                print(f"{'ok ' if ok else 'BAD'} {task.id:<15} {label:<9} hidden tests "
                      f"{'pass' if passed else 'fail'}")
            continue
        if task.id in REFERENCE_SOLVERS:
            box = Sandbox(task.files)
            try:
                printed = box.bash(REFERENCE_SOLVERS[task.id]).strip()
            finally:
                box.close()
            ok = task.grade_answer(printed) and not task.grade_answer("0")
            failures += not ok
            print(f"{'ok ' if ok else 'BAD'} {task.id:<15} reference answer {printed!r}")
        if task.id in GOOD_BAD:
            good, bad = GOOD_BAD[task.id]
            ok = task.grade_answer(good) and not task.grade_answer(bad)
            failures += not ok
            print(f"{'ok ' if ok else 'BAD'} {task.id:<15} grader: good accepted, bad rejected")
    print("selfcheck passed" if not failures else f"selfcheck: {failures} problem(s)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.calibration.run")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selfcheck")
    for name in ("hle", "judge-export"):
        p = sub.add_parser(name)
        p.add_argument("--parquet", required=True)
        p.add_argument("--n", type=int, default=20)
    p_live = sub.add_parser("live")
    p_live.add_argument("--tasks", default="")
    for p in (sub.choices["hle"], p_live):
        p.add_argument("--conditions", default=",".join(prompts.CONDITIONS))
        p.add_argument("--repeat", default="1",
                       help="times per condition: a number, or e.g. baseline=2 (others once)")
        p.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    if args.command == "selfcheck":
        return selfcheck()
    spec = getattr(args, "repeat", "1")  # judge-export has no --repeat
    repeat = (int(spec) if spec.isdigit() else
              {c: int(n) for c, n in (part.split("=") for part in spec.split(","))})
    if args.command == "live":
        chosen = [t for t in tasks.all_tasks() if not args.tasks or t.id in args.tasks.split(",")]
        agent.run(chosen, args.conditions.split(","), RAW, workers=args.workers, repeat=repeat)
        return 0
    items = hle.sample(hle.load_items(args.parquet), args.n)
    if args.command == "hle":
        hle.run(items, args.conditions.split(","), RAW, workers=args.workers, repeat=repeat)
        return 0
    count = hle.export_for_judge(items, RAW, RAW / "to_judge.jsonl")
    print(f"{count} answers written to {RAW / 'to_judge.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

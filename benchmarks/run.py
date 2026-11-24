#!/usr/bin/env python3
"""Compare models through the bridge on the same suites, prompts and scoring.

    ./benchmarks/run.py --model agentaus
    ./benchmarks/run.py --model agentaus --model claude-opus-5 --suite humaneval --limit 20
    ./benchmarks/run.py --list

Everything goes through the local bridge, which routes on the request's `model` field. So
the two arms of a comparison share the harness exactly and differ only in who answered -
which is the one thing a benchmark has to get right and the reason this is cheap here.

A resolve rate alone is not reported. A model that scores two points higher for four times
the tokens and three times the wall-clock has not won anything a deployment cares about,
so tokens and latency sit beside every score.

A second arm is optional. HumanEval is reported widely enough that a reference point is a
lookup rather than an experiment, so `baselines.py` carries published figures and the run
places the measured score among them - no API key, no spend. Where a real head-to-head is
wanted, `--model claude-opus-5` works, but the bridge forwards those requests to
api.anthropic.com and a harness cannot borrow the OAuth token Claude Code holds, so it
needs ANTHROPIC_API_KEY and says so plainly rather than reporting a zero.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import harness  # noqa: E402
import humaneval  # noqa: E402
import baselines  # noqa: E402
import coverage  # noqa: E402
import documents  # noqa: E402
import injection  # noqa: E402
import retrieval  # noqa: E402

SUITES = {
    "humaneval": (humaneval.run, "164 problems with their own unit tests. pass@1."),
    "retrieval": (retrieval.run, "Does it find the right file? Precision, recall, F1."),
    "injection": (injection.run, "Does text inside a file hijack it? Resist rate."),
    "documents": (documents.run, "Can it answer from a PDF? Found and cited rates."),
    "coverage": (coverage.run, "Does it survey the corpus or answer from two files?"),
}

# Coverage is an A/B, so its headline is the difference the procedure made. Reporting
# the skill arm's count alone gave a number with no target, which is what the suite says
# it must not do.
HEADLINE = {"humaneval": ("pass_at_1", "pass@1"),
            "retrieval": ("f1", "F1"),
            "injection": ("resist_rate", "resisted"),
            "documents": ("grounded_rate", "found+cited"),
            "coverage": ("delta", "+files")}

REPO = Path(__file__).resolve().parent.parent

# The bridge reports the usage of its final upstream call. The planner, the grounding
# check and the model calls inside agentaus_search are not in it, so an Agentaus arm's
# tokens are a floor, and comparing them with a Claude arm's flatters Agentaus.
TOKEN_NOTE = ("tokens: an Agentaus arm's are the final upstream call only - the bridge's "
              "helper calls and agentaus_search are not counted, so read them as a floor")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", default=[],
                        help="repeatable; e.g. agentaus, claude-opus-5")
    parser.add_argument("--suite", action="append", default=[],
                        help=f"repeatable; one of {', '.join(SUITES)} (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="cases per suite - start small, these cost real calls")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"),
                        help="directory for the JSON record of the run")
    parser.add_argument("--root", default=str(REPO),
                        help="repository the retrieval questions are about (default: "
                             "this one, wherever the run is started from)")
    parser.add_argument("--corpus", default=None,
                        help="tender folder for documents and coverage (default: "
                             "$BENCH_CORPUS, else the path the suites were written on)")
    parser.add_argument("--list", action="store_true", help="list suites and exit")
    args = parser.parse_args()

    if args.list:
        for name, (_fn, blurb) in SUITES.items():
            print(f"  {name:11} {blurb}")
        return 0

    models = args.model or ["agentaus"]
    chosen = args.suite or list(SUITES)
    unknown = [s for s in chosen if s not in SUITES]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    context = {"retrieval": {"root": args.root},
               "documents": {"corpus": args.corpus},
               "coverage": {"corpus": args.corpus}}
    results: dict = {"_meta": {**harness.run_metadata(), "argv": sys.argv[1:],
                               "started": time.strftime("%Y-%m-%dT%H:%M:%S")}}
    for model in models:
        problem = harness.preflight(model)
        if problem:
            print(f"\n=== {model}: SKIPPED ===\n  {problem}\n")
            results[model] = {"skipped": problem}
            continue

        results[model] = {}
        for suite in chosen:
            run_suite, _blurb = SUITES[suite]
            totals = harness.Totals()
            print(f"\n=== {model} / {suite} ===")
            started = time.monotonic()
            outcome = run_suite(model, args.limit, harness.ask, totals,
                                **context.get(suite, {}))
            if "skipped" in outcome:
                results[model][suite] = outcome
                continue
            outcome["wall_seconds"] = time.monotonic() - started
            outcome["calls"] = totals.calls
            outcome["call_failures"] = totals.failures
            outcome["rounds_exhausted"] = totals.exhausted
            outcome["input_tokens"] = totals.input_tokens
            outcome["output_tokens"] = totals.output_tokens
            outcome["median_call_seconds"] = totals.median_seconds
            results[model][suite] = outcome

    print("\n" + "=" * 78)
    print("COMPARISON")
    print("=" * 78)
    print(f"  ({TOKEN_NOTE})")
    for suite in chosen:
        rows = [(m, r[suite]) for m, r in results.items()
                if m != "_meta" and suite in r]
        if not rows:
            continue
        key, label = HEADLINE[suite]
        print(f"\n{suite}")
        print(f"  {'model':18} {label:>9} {'n':>4} {'ans':>4} {'calls':>6} {'fail':>5} "
              f"{'cut':>4} {'in tok':>9} {'out tok':>8} {'med s':>7} {'wall s':>7}")
        for model, r in rows:
            if "skipped" in r:
                print(f"  {model:18} SKIPPED - {r['skipped']}")
                continue
            print(f"  {model:18} {r[key]:>9.3f} {r['n']:>4} {r['answered']:>4} "
                  f"{r['calls']:>6} {r['call_failures']:>5} {r['rounds_exhausted']:>4} "
                  f"{r['input_tokens']:>9,} {r['output_tokens']:>8,} "
                  f"{r['median_call_seconds']:>7.1f} {r['wall_seconds']:>7.0f}")
        rows = [(m, r) for m, r in rows if "skipped" not in r]
        if suite == "humaneval":
            print()
            print(baselines.table())
            for model, r in rows:
                print(f"    -> {model} at {r[key]:.3f} sits in: "
                      f"{baselines.bracket(r[key])}")
        elif suite in baselines.LOCAL_ONLY:
            print(f"    ({baselines.LOCAL_ONLY[suite]})")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    path = out / f"run_{stamp}.json"
    path.write_text(json.dumps(results, indent=1, default=str))
    print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

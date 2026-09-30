#!/usr/bin/env python3
"""Aggregate benchmark runs into one JSON summary, with uncertainty.

    ./summarise.py ctest ctest_r1 ctest_r2 ctest_r3 > results/ctest.json

Scores are proportions (problems passed, questions right), so each carries a 95% Wilson
interval: at n=82 a two-point gap is well inside the noise, and a table without the
interval invites reading one as a win.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

RUNS = Path("/Users/cheng/agentaus_ab_runs")


def wilson(k: float, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def load(labels):
    recs = []
    for label in labels:
        for p in (RUNS / label).glob("*/*.json"):
            try:
                r = json.loads(p.read_text())
            except ValueError:
                continue
            # Only run records. A model that wrote its answers one directory too high
            # leaves an answers.json beside them, which is not one.
            if not isinstance(r, dict) or "suite" not in r or "grade" not in r:
                continue
            r["_label"] = label
            recs.append(r)
    return recs


def summarise(recs):
    out = {}
    groups = {}
    for r in recs:
        groups.setdefault((r["suite"], r["arm"]), []).append(r)
    for (suite, arm), rs in sorted(groups.items()):
        infra = [r for r in rs if r.get("infra_error")]
        rs = [r for r in rs if not r.get("infra_error")]
        if not rs:
            continue
        if suite in ("mmlu", "mmlu_pro"):
            k = sum(r["grade"]["right"] for r in rs)
            n = sum(r["grade"]["n"] for r in rs)
            no_file = sum(1 for r in rs if not r["grade"].get("answers_file"))
            extra = {"batches": len(rs), "batches_without_answers_file": no_file}
        elif suite == "he":
            k = sum(1 for r in rs if r["grade"]["score"] == 1.0)
            n = len(rs)
            extra = {"base_tests_pass": sum(1 for r in rs if r["grade"].get("base")) / n,
                     "failed": sorted(r["task"] for r in rs if r["grade"]["score"] != 1.0)}
        else:
            k = sum(r["grade"]["score"] for r in rs)
            n = len(rs)
            per_task = {}
            for r in rs:
                per_task.setdefault(r["task"], []).append(round(r["grade"]["score"], 3))
            extra = {"per_task": per_task}
        walls = sorted(r["wall_s"] for r in rs)
        lo, hi = wilson(k, n)
        out.setdefault(suite, {})[arm] = {
            "score": k / n, "k": k, "n": n, "ci95": [lo, hi],
            "wall_p50": statistics.median(walls),
            "wall_p90": walls[min(len(walls) - 1, int(0.9 * len(walls)))],
            "turns_mean": statistics.mean([r.get("num_turns") or 0 for r in rs]),
            "timeouts": sum(r["timed_out"] for r in rs),
            "infra_excluded": len(infra),
            **extra,
        }
    return out


if __name__ == "__main__":
    print(json.dumps(summarise(load(sys.argv[1:])), indent=1))

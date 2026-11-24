"""The expansion's own numbers: the new questions on their own, and how much the baseline
varies between two runs of the same questions.

    python -m benchmarks.calibration.expansion RAW_DIR PILOT_RAW_DIR OUT_DIR [N_BINS]

The prompts kept for the expansion were picked on the pilot's questions, so only the
questions the pilot did not use give an unbiased estimate of their effect. Every prompt is
compared with each baseline run separately: if a prompt's change is no bigger than the
change between the two baseline runs, it is not distinguishable from run-to-run noise.
Writes OUT_DIR/expansion.json (IDs and scores only, like the rest of the public data).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import metrics, report

METRICS = ("rms", "ece", "brier", "overconfidence", "accuracy")


def _by_id(rows: list[dict]) -> dict:
    return {r["id"]: (r["confidence"], r["correct"]) for r in rows}


def _delta(base: list[dict], other: list[dict], n_bins: int) -> dict:
    base, other = _by_id(base), _by_id(other)
    fns = report._metric_fns(n_bins)
    out = {"n_paired": len(set(base) & set(other))}
    if out["n_paired"]:
        out.update({name: metrics.paired_bootstrap_diff(base, other, fns[name])
                    for name in METRICS})
    return out


def _agreement(first: list[dict], second: list[dict]) -> dict:
    """Per question, whether two runs of the same prompt were right the same way."""
    a, b = _by_id(first), _by_id(second)
    ids = sorted(set(a) & set(b))
    both = sum(a[i][1] and b[i][1] for i in ids)
    either = sum(bool(a[i][1]) != bool(b[i][1]) for i in ids)
    gaps = [abs(a[i][0] - b[i][0]) for i in ids]
    return {"n": len(ids), "both_correct": both, "one_correct": either,
            "neither_correct": len(ids) - both - either,
            "mean_abs_confidence_change": sum(gaps) / len(gaps),
            "confidence_change_over_0.2": sum(g > 0.2 for g in gaps),
            "both_forced": sum(1 for i in ids if _forced(first, i) and _forced(second, i))}


def _forced(rows: list[dict], item_id: str) -> bool:
    return any(r["id"] == item_id and r["forced"] for r in rows)


def analyse(hle_rows: list[dict], pilot_ids: set, n_bins: int) -> dict:
    runs: dict[tuple, list[dict]] = {}
    for r in hle_rows:
        runs.setdefault((r["condition"], r.get("repeat", 0)), []).append(r)
    baselines = sorted(k for k in runs if k[0] == "baseline")
    out: dict = {"n_bins": n_bins, "subsets": {}}
    for subset, keep in (("all", lambda r: True), ("new", lambda r: r["id"] not in pilot_ids),
                         ("pilot", lambda r: r["id"] in pilot_ids)):
        block = {}
        for (condition, repeat), rows in sorted(runs.items()):
            rows = [r for r in rows if keep(r)]
            kept = [r for r in rows if not r["forced"]]
            entry = report._stats(rows, n_bins)
            entry["excluding_forced"] = report._stats(kept, n_bins, intervals=False)
            entry["vs"] = {}
            for base in baselines:
                if base == (condition, repeat):
                    continue
                base_rows = [r for r in runs[base] if keep(r)]
                entry["vs"][f"baseline_{base[1]}"] = _delta(base_rows, rows, n_bins)
                entry["vs"][f"baseline_{base[1]}"]["excluding_forced"] = _delta(
                    [r for r in base_rows if not r["forced"]], kept, n_bins)
            block[f"{condition}_{repeat}"] = entry
        if len(baselines) == 2:
            block["baseline_agreement"] = _agreement(
                *[[r for r in runs[b] if keep(r)] for b in baselines])
        out["subsets"][subset] = block
    return out


def figure(result: dict, path: Path) -> None:
    """Change in RMS against each baseline run, beside the change between the two runs."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": report.SURFACE})
    panels = (("all", "All 80 questions"), ("new", "60 questions new to the expansion"))
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.8), sharey=True)
    rows = [("baseline_1", "baseline run 2")] + [
        (f"{c}_0", report.LABELS[c]) for c in ("limits", "frequency", "combined")]
    ys = range(len(rows))[::-1]
    for ax, (subset, title) in zip(axes, panels):
        block = result["subsets"][subset]
        for y, (name, _) in zip(ys, rows):
            series = [("baseline_0", 0.14, report.BLUE)]
            if name != "baseline_1":
                series.append(("baseline_1", -0.14, report.ORANGE))
            for base, dy, colour in series:
                d, lo, hi = block[name]["vs"][base]["rms"]
                colour = report.INK_2 if name == "baseline_1" else colour
                ax.plot([lo, hi], [y + dy] * 2, color=colour, linewidth=2,
                        solid_capstyle="round")
                report._dot(ax, d, y + dy, colour)
        ax.axvline(0, color=report.INK_2, linewidth=1)
        ax.set_yticks(list(ys), [label for _, label in rows])
        report._axes(ax, title)
    fig.supxlabel("change in RMS calibration error (left of 0 = better; 95% CI, paired)",
                  color=report.INK_2, fontsize=9, y=0.07)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    fig.text(0.01, 0.01, "blue: against baseline run 1   orange: against baseline run 2   "
             "grey: baseline run 2 against run 1 (run-to-run noise)",
             color=report.INK_2, fontsize=8)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    raw, pilot_raw, out = Path(argv[0]), Path(argv[1]), Path(argv[2])
    n_bins = int(argv[3]) if len(argv) > 3 else 10
    hle_rows, _, unjudged = report.load(raw)
    if unjudged:
        print(f"WARNING: HLE answers without a verdict, left out: {unjudged}", file=sys.stderr)
    pilot_ids = {r["id"] for r in report._jsonl(pilot_raw / "hle_baseline.jsonl")}
    result = analyse(hle_rows, pilot_ids, n_bins)
    out.mkdir(parents=True, exist_ok=True)
    (out / "expansion.json").write_text(json.dumps(result, indent=1))
    figure(result, out / "figures" / "expansion_deltas.png")
    for subset, block in result["subsets"].items():
        print(f"\n{subset}")
        for name, e in block.items():
            if name == "baseline_agreement":
                print("  agreement", e)
                continue
            line = (f"  {name:12} n={e['n']:3} acc={e['accuracy']:.3f} "
                    f"conf={e['mean_confidence']:.2f} rms={e['rms_calib_err']:.3f} "
                    f"brier={e['brier']:.3f} forced={e['forced']}")
            for base, d in e["vs"].items():
                if "rms" in d:
                    rms, brier = d["rms"], d["brier"]
                    line += (f" | vs {base}: rms {rms[0]:+.3f} [{rms[1]:+.3f},{rms[2]:+.3f}]"
                             f" brier {brier[0]:+.3f} [{brier[1]:+.3f},{brier[2]:+.3f}]")
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

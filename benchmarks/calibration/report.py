"""Score a run, write the public data and draw the figures.

    python -m benchmarks.calibration.report RAW_DIR OUT_DIR [N_BINS]

Public data (OUT_DIR/*.csv, summary.json) holds HLE question IDs and scores only - no
question text, reference answers or model responses, which are HLE data under its
canary and stay in RAW_DIR.

The two suites are reported separately; the live suite is near its ceiling, so a pooled
number would mostly reward confidence near 100%. Every metric is given twice: as
answered (the headline, with forced answers re-asked) and without the forced answers -
HLE answers that ran out of tokens, live runs that hit the step or time limit.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from . import metrics, prompts, tasks

SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE = "#2a78d6", "#eb6834"
LABELS = {"baseline": "baseline", "limits": "1 know limits", "flag": "2 flag limits",
          "verify": "3 verify first", "frequency": "4 frequency", "opposite": "5 opposite",
          "candidates": "6 candidates", "combined": "7 combined"}
SUITES = {"hle": "HLE-Diamond", "live": "Live agentic"}


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def load(raw: Path) -> tuple[list[dict], list[dict], dict]:
    """HLE rows with verdicts, live rows, and the count of HLE rows still unjudged per
    condition (those are left out of every metric, so they are reported, not dropped)."""
    keymap = json.loads((raw / "hle_judge_keys.json").read_text()) \
        if (raw / "hle_judge_keys.json").exists() else {}
    verdicts = {}
    for v in _jsonl(raw / "hle_judgements.jsonl"):
        condition, item_id, repeat = keymap[v["key"]]
        verdicts[(condition, item_id, repeat)] = v["correct"].strip().lower() == "yes"
    hle_rows, unjudged = [], {}
    for condition in prompts.CONDITIONS:
        for row in _jsonl(raw / f"hle_{condition}.jsonl"):
            key = (row["condition"], row["id"], row.get("repeat", 0))
            if key in verdicts:
                hle_rows.append({**row, "correct": int(verdicts[key]), "suite": "hle",
                                 "forced": bool(row["truncated"])})
            else:
                unjudged[condition] = unjudged.get(condition, 0) + 1
    # Answer graders are regraded from the stored Answer line, so a grader fixed after a
    # run applies to every row alike; a changed grade is marked.
    graders = {t.id: t.grade_answer for t in tasks.all_tasks() if t.kind != "coding"}
    live_rows = []
    for condition in prompts.CONDITIONS:
        for r in _jsonl(raw / f"live_{condition}.jsonl"):
            row = {**r, "suite": "live", "forced": bool(r["hit_limit"])}
            if r["id"] in graders:
                row["correct"] = int(bool(graders[r["id"]](r["answer"])))
                row["regraded"] = row["correct"] != r["correct"]
            live_rows.append(row)
    return hle_rows, live_rows, unjudged


def _key(row: dict) -> tuple:
    return (row["suite"], row["id"], row.get("repeat", 0))


def _overconfidence(c: list, k: list) -> float:
    return sum(c) / len(c) - sum(k) / len(k)


def _metric_fns(n_bins: int) -> dict:
    return {"rms": lambda c, k: metrics.rms_calib_err(c, k, n_bins),
            "ece": lambda c, k: metrics.ece(c, k, n_bins),
            "brier": metrics.brier,
            "overconfidence": _overconfidence,
            "accuracy": lambda c, k: sum(k) / len(k)}


def _stats(rows: list[dict], n_bins: int, *, intervals: bool = True) -> dict:
    conf = [r["confidence"] for r in rows]
    corr = [r["correct"] for r in rows]
    out = metrics.summary(conf, corr, n_bins=n_bins)
    if intervals:
        for name in ("rms", "ece", "brier", "overconfidence"):
            out[f"{name}_ci"] = metrics.bootstrap(conf, corr, _metric_fns(n_bins)[name])
    out["cost_aud"] = sum(r["cost_aud"] for r in rows)
    out["mean_output_tokens"] = sum(r["output_tokens"] for r in rows) / len(rows)
    out["confidence_missing"] = sum(bool(r.get("confidence_missing")) for r in rows)
    out["forced"] = sum(r["forced"] for r in rows)
    return out


def _deltas(base_rows: list[dict], rows: list[dict], n_bins: int) -> dict:
    base = {_key(r): (r["confidence"], r["correct"]) for r in base_rows}
    other = {_key(r): (r["confidence"], r["correct"]) for r in rows}
    out = {"n_paired": len(set(base) & set(other)), "n_baseline": len(base), "n": len(other)}
    if out["n_paired"]:
        for name, fn in _metric_fns(n_bins).items():
            out[name] = metrics.paired_bootstrap_diff(base, other, fn)
    return out


def summarise(hle_rows: list[dict], live_rows: list[dict], unjudged: dict, n_bins: int) -> dict:
    summary: dict = {"n_bins": n_bins, "hle_unjudged": unjudged, "suites": {}}
    for suite, rows in (("hle", hle_rows), ("live", live_rows)):
        per = {c: [r for r in rows if r["condition"] == c] for c in prompts.CONDITIONS}
        per = {c: v for c, v in per.items() if v}
        if not per:
            continue
        block = {}
        for c, v in per.items():
            kept = [r for r in v if not r["forced"]]
            block[c] = _stats(v, n_bins)
            block[c]["excluding_forced"] = _stats(kept, n_bins) if kept else None
            if c != "baseline" and "baseline" in per:
                block[c]["delta"] = _deltas(per["baseline"], v, n_bins)
                if kept:
                    block[c]["excluding_forced"]["delta"] = _deltas(
                        [r for r in per["baseline"] if not r["forced"]], kept, n_bins)
        summary["suites"][suite] = block
        summary[f"{suite}_forced_sensitivity"] = _forced_sensitivity(rows, n_bins)
        summary[f"{suite}_final_paths"] = {
            c: {p: sum(1 for r in v if r.get("final_path", "direct") == p)
                for p in ("direct", "reask_high", "reask_none", "missing")}
            for c, v in per.items()}
    if live_rows:
        summary["live_by_kind"] = {
            kind: {c: _stats([r for r in live_rows if r["kind"] == kind and r["condition"] == c],
                             n_bins, intervals=False)
                   for c in prompts.CONDITIONS
                   if any(r["kind"] == kind and r["condition"] == c for r in live_rows)}
            for kind in ("coding", "tools", "browser")}
    # Kept for reference only: not a headline, for the reason in the module docstring.
    both = hle_rows + live_rows
    summary["pooled_reference_only"] = {
        c: metrics.summary([r["confidence"] for r in both if r["condition"] == c],
                           [r["correct"] for r in both if r["condition"] == c], n_bins=n_bins)
        for c in prompts.CONDITIONS if any(r["condition"] == c for r in both)}
    return summary


def _forced_sensitivity(rows: list[dict], n_bins: int) -> dict:
    """Forced answers (out of tokens, or at the step/time limit) taken as re-asked, set to
    50% confidence, or left out."""
    out = {}
    for c in prompts.CONDITIONS:
        v = [r for r in rows if r["condition"] == c]
        if not v:
            continue
        forced = [r for r in v if r["forced"]]
        out[c] = {"forced_rate": len(forced) / len(v), "n": len(v),
                  "forced_correct": sum(r["correct"] for r in forced)}
        for label, sample in (("reasked", v),
                              ("imputed_50", [{**r, "confidence": 0.5} if r["forced"] else r for r in v]),
                              ("excluded", [r for r in v if not r["forced"]])):
            if sample:
                conf = [r["confidence"] for r in sample]
                corr = [r["correct"] for r in sample]
                out[c][label] = {"n": len(sample),
                                 "rms_calib_err": metrics.rms_calib_err(conf, corr, n_bins),
                                 "ece": metrics.ece(conf, corr, n_bins),
                                 "brier": metrics.brier(conf, corr)}
    return out


def write_public(hle_rows: list[dict], live_rows: list[dict], out: Path) -> None:
    hle_fields = ["id", "condition", "repeat", "partition", "answer_type", "category", "correct",
                  "confidence", "confidence_missing", "truncated", "reasked", "final_path", "output_tokens",
                  "cost_aud"]
    live_fields = ["id", "kind", "condition", "repeat", "correct", "confidence",
                   "confidence_missing", "answer_missing", "hit_limit", "reasked", "final_path",
                   "tool_calls", "steps", "truncations", "output_tokens", "cost_aud", "seconds",
                   "working_seconds"]
    for name, rows, fields in (("scores_hle.csv", hle_rows, hle_fields),
                               ("scores_live.csv", live_rows, live_fields)):
        with (out / name).open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in sorted(rows, key=lambda r: (r["condition"], r["id"], r.get("repeat", 0))):
                writer.writerow({k: (round(row[k], 6) if isinstance(row.get(k), float) else row.get(k))
                                 for k in fields})


# ------------------------------------------------------------------------- figures
# Filled markers: as answered (headline). Hollow markers: forced answers left out.

def _axes(ax, title: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=11, pad=10)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _dot(ax, x, y, colour, hollow=False, **kw):
    ax.plot(x, y, "o", markersize=8 if not hollow else 7, markeredgewidth=2,
            color=colour, markerfacecolor=SURFACE if hollow else colour,
            markeredgecolor=colour if hollow else SURFACE, **kw)


def _per_suite(plt, summary: dict, width: float, height: float):
    suites = [s for s in SUITES if s in summary["suites"]]
    fig, axes = plt.subplots(1, len(suites), figsize=(width * len(suites), height), sharey=True)
    return fig, (list(axes) if len(suites) > 1 else [axes]), suites


def _legend_note(fig) -> None:
    fig.text(0.01, 0.01, "filled: as answered (forced answers re-asked)   hollow: forced answers left out",
             color=INK_2, fontsize=8)


def figures(summary: dict, hle_rows: list[dict], live_rows: list[dict], out: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SURFACE})
    out.mkdir(parents=True, exist_ok=True)
    made = []

    # 1. RMS calibration error per condition, with 95% bootstrap intervals.
    fig, axes, suites = _per_suite(plt, summary, 4.4, 4.0)
    for ax, suite in zip(axes, suites):
        block = summary["suites"][suite]
        conds = [c for c in prompts.CONDITIONS if c in block]
        ys = range(len(conds))[::-1]
        for y, c in zip(ys, conds):
            lo, hi = block[c]["rms_ci"]
            ax.plot([lo, hi], [y, y], color=BLUE, linewidth=2, solid_capstyle="round")
            _dot(ax, block[c]["rms_calib_err"], y, BLUE)
            if block[c]["excluding_forced"]:
                _dot(ax, block[c]["excluding_forced"]["rms_calib_err"], y - 0.28, BLUE, hollow=True)
        if "baseline" in block:
            ax.axvline(block["baseline"]["rms_calib_err"], color=INK_2, linewidth=1, linestyle=(0, (3, 3)))
        ax.set_yticks(list(ys), [LABELS[c] for c in conds])
        ax.set_xlim(0, 1)
        ax.set_xlabel("RMS calibration error (lower is better)", color=INK_2, fontsize=9)
        _axes(ax, SUITES[suite])
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    _legend_note(fig)
    fig.savefig(out / "calibration_error.png", dpi=160)
    plt.close(fig)
    made.append("calibration_error.png")

    # 2. Mean confidence against accuracy.
    fig, axes, suites = _per_suite(plt, summary, 4.4, 4.0)
    for ax, suite in zip(axes, suites):
        block = summary["suites"][suite]
        conds = [c for c in prompts.CONDITIONS if c in block]
        ys = range(len(conds))[::-1]
        for y, c in zip(ys, conds):
            for stats, dy, hollow in ((block[c], 0, False), (block[c]["excluding_forced"], -0.28, True)):
                if not stats:
                    continue
                acc, conf = stats["accuracy"], stats["mean_confidence"]
                ax.plot([acc, conf], [y + dy] * 2, color=GRID, linewidth=2, zorder=1)
                first = y == len(conds) - 1 and not hollow
                _dot(ax, acc, y + dy, BLUE, hollow, zorder=2, label="accuracy" if first else None)
                _dot(ax, conf, y + dy, ORANGE, hollow, zorder=2,
                     label="mean stated confidence" if first else None)
        ax.set_yticks(list(ys), [LABELS[c] for c in conds])
        ax.set_xlim(0, 1.03)
        ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        _axes(ax, f"Confidence vs accuracy: {SUITES[suite]}")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    _legend_note(fig)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower right", ncol=2, frameon=False, fontsize=8,
               labelcolor=INK_2)
    fig.savefig(out / "confidence_vs_accuracy.png", dpi=160)
    plt.close(fig)
    made.append("confidence_vs_accuracy.png")

    # 3. Paired change in RMS calibration error against the baseline.
    fig, axes, suites = _per_suite(plt, summary, 4.4, 3.8)
    for ax, suite in zip(axes, suites):
        block = summary["suites"][suite]
        conds = [c for c in prompts.CONDITIONS if c in block and "delta" in block[c]
                 and "rms" in block[c]["delta"]]
        ys = range(len(conds))[::-1]
        for y, c in zip(ys, conds):
            d, lo, hi = block[c]["delta"]["rms"]
            ax.plot([lo, hi], [y, y], color=BLUE, linewidth=2, solid_capstyle="round")
            _dot(ax, d, y, BLUE)
            ex = block[c]["excluding_forced"]
            if ex and "rms" in ex.get("delta", {}):
                _dot(ax, ex["delta"]["rms"][0], y - 0.28, BLUE, hollow=True)
        ax.axvline(0, color=INK_2, linewidth=1)
        ax.set_yticks(list(ys), [LABELS[c] for c in conds])
        ax.set_xlabel("change in RMS calibration error vs baseline\n(left of 0 = better; 95% CI, paired)",
                      color=INK_2, fontsize=9)
        _axes(ax, SUITES[suite])
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    _legend_note(fig)
    fig.savefig(out / "delta_vs_baseline.png", dpi=160)
    plt.close(fig)
    made.append("delta_vs_baseline.png")

    # 4. Reliability: every condition against the baseline, one panel each, per suite -
    # all of them, so no condition is singled out by the data being plotted.
    for suite, rows in (("hle", hle_rows), ("live", live_rows)):
        conds = [c for c in prompts.CONDITIONS if c != "baseline" and any(r["condition"] == c for r in rows)]
        if not conds:
            continue
        cols = 4
        nrows = (len(conds) + cols - 1) // cols
        fig, grid = plt.subplots(nrows, cols, figsize=(3.0 * cols, 3.0 * nrows + 0.4),
                                 sharex=True, sharey=True, squeeze=False)
        for ax in grid.flat[len(conds):]:
            ax.set_visible(False)
        for ax, c in zip(grid.flat, conds):
            ax.plot([0, 1], [0, 1], color=GRID, linewidth=1, linestyle=(0, (3, 3)))
            for cond, colour in (("baseline", INK_2), (c, BLUE)):
                for hollow in (False, True):
                    v = [r for r in rows if r["condition"] == cond and not (hollow and r["forced"])]
                    xs, ys_, sizes = _reliability_points(v)
                    ax.plot(xs, ys_, color=colour, linewidth=1.5 if not hollow else 1,
                            linestyle="-" if not hollow else (0, (2, 2)))
                    if not hollow:
                        ax.scatter(xs, ys_, s=[12 + 3 * s for s in sizes], color=colour,
                                   edgecolors=SURFACE, linewidths=1.5, zorder=3)
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            _axes(ax, f"{LABELS[c]} (blue) vs baseline")
            ax.title.set_fontsize(9)
            ax.grid(axis="y", color=GRID, linewidth=0.8)
        fig.suptitle(f"Reliability, {SUITES[suite]}: share correct by stated confidence "
                     "(dashed: forced answers left out)", x=0.01, ha="left", color=INK, fontsize=10)
        fig.supxlabel("stated confidence (bin mean; dot size = answers)", color=INK_2, fontsize=9)
        fig.supylabel("share correct", color=INK_2, fontsize=9)
        fig.tight_layout()
        name = f"reliability_{suite}.png"
        fig.savefig(out / name, dpi=160)
        plt.close(fig)
        made.append(name)

    # 5. Cost per answer.
    fig, axes, suites = _per_suite(plt, summary, 4.4, 3.6)
    for ax, suite in zip(axes, suites):
        block = summary["suites"][suite]
        conds = [c for c in prompts.CONDITIONS if c in block]
        ys = range(len(conds))[::-1]
        per_answer = [block[c]["cost_aud"] / block[c]["n"] for c in conds]
        ax.barh(list(ys), per_answer, color=BLUE, height=0.6)
        for y, v in zip(ys, per_answer):
            ax.text(v, y, f"  {v:.3f}", va="center", color=INK_2, fontsize=8)
        ax.set_yticks(list(ys), [LABELS[c] for c in conds])
        ax.set_xlabel("mean cost per answer (AUD)", color=INK_2, fontsize=9)
        _axes(ax, SUITES[suite])
    fig.tight_layout()
    fig.savefig(out / "cost.png", dpi=160)
    plt.close(fig)
    made.append("cost.png")
    return made


def _reliability_points(rows: list[dict], bins: int = 5) -> tuple[list, list, list]:
    xs, ys, sizes = [], [], []
    for b in range(bins):
        chunk = [r for r in rows if b / bins <= r["confidence"] < (b + 1) / bins
                 or (b == bins - 1 and r["confidence"] == 1)]
        if chunk:
            xs.append(sum(r["confidence"] for r in chunk) / len(chunk))
            ys.append(sum(r["correct"] for r in chunk) / len(chunk))
            sizes.append(len(chunk))
    return xs, ys, sizes


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    raw, out = Path(argv[0]), Path(argv[1])
    n_bins = int(argv[2]) if len(argv) > 2 else 10
    out.mkdir(parents=True, exist_ok=True)
    hle_rows, live_rows, unjudged = load(raw)
    if unjudged:
        print(f"WARNING: HLE answers without a verdict, left out of every metric: {unjudged}",
              file=sys.stderr)
    summary = summarise(hle_rows, live_rows, unjudged, n_bins)
    write_public(hle_rows, live_rows, out)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    made = figures(summary, hle_rows, live_rows, out / "figures")
    print(f"{len(hle_rows)} HLE + {len(live_rows)} live answers -> {out} ({', '.join(made)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

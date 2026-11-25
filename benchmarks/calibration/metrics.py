"""Calibration metrics: HLE's RMS calibration error, ECE and Brier, with bootstrap CIs.

`hle_calib_err` is hle_eval/run_judge_results.py's calib_err, ported line for line
(including that it skips the last bin), except that tied confidences score at their
group's average where HLE's argsort leaves them in input order. With few ties a full run's
number is HLE's number.
Confidences are probabilities in [0, 1]; correct is 0 or 1.
"""
from __future__ import annotations

import random
import re


_CONFIDENCE = re.compile(
    r"confidence(?:\s+(?:level|score|estimate|rating))?\**\s*[:=]\s*\**\s*"
    r"(?:~|≈|about|around|approx(?:imately|\.)?)?\s*(\d{1,3}(?:\.\d+)?)\s*(%)?", re.I)


def parse_confidence(text: str) -> float | None:
    """The last 'Confidence: N%' in a response, as a probability; None if absent.

    Also reads 'Confidence level: 80%', '~80%' and a bare probability such as '0.85'
    (a value of at most 1 with a decimal point and no % sign).
    """
    found = _CONFIDENCE.findall(text or "")
    if not found:
        return None
    number, percent = found[-1]
    value = float(number)
    if not percent and value <= 1 and "." in number:
        return value
    return min(100.0, value) / 100.0


def parse_answer(text: str) -> str:
    found = re.findall(r"^\s*\**answer\**\s*[:=]\s*(.+)$", text or "", re.I | re.M)
    return found[-1].strip() if found else ""


def _by_confidence(confidence: list, correct: list) -> list[tuple]:
    """(confidence, correct) pairs ordered by confidence, each tied group scored at its
    average correctness.

    Sorting the pairs themselves would order tied confidences by correctness and split
    them into separate right and wrong bins. Breaking ties by a seeded shuffle instead made
    the result depend on input order, so the same answers listed in another order (as a
    paired bootstrap lists them) scored up to 0.015 apart. A tie group's average is what
    any bin it spills into gets on average over every order of the tie.
    """
    pairs = sorted(zip(confidence, correct), key=lambda p: p[0])
    out, start = [], 0
    for end in range(1, len(pairs) + 1):
        if end == len(pairs) or pairs[end][0] != pairs[start][0]:
            group = pairs[start:end]
            mean = sum(k for _, k in group) / len(group)
            out.extend((c, mean) for c, _ in group)
            start = end
    return out


def hle_calib_err(confidence: list, correct: list, beta: int = 100) -> float:
    pairs = _by_confidence(confidence, correct)
    n = len(pairs)
    if n // beta < 2:
        raise ValueError(f"need at least {2 * beta} answers for bins of {beta}")
    bins = [[i * beta, (i + 1) * beta] for i in range(n // beta)]
    bins[-1] = [bins[-1][0], n]
    total = 0.0
    for lo, hi in bins[:-1]:                      # as in HLE: the last bin is not counted
        chunk = pairs[lo:hi]
        conf = sum(c for c, _ in chunk) / len(chunk)
        acc = sum(k for _, k in chunk) / len(chunk)
        total += len(chunk) / n * (conf - acc) ** 2
    return total ** 0.5


def rms_calib_err(confidence: list, correct: list, n_bins: int = 10) -> float:
    """RMS calibration error over equal-count bins, every bin counted.

    The pilot's version of HLE's metric: HLE's bins of 100 need hundreds of answers per
    condition, so the pilot uses n_bins equal-count bins instead.
    """
    pairs = _by_confidence(confidence, correct)
    n = len(pairs)
    edges = [round(i * n / n_bins) for i in range(n_bins + 1)]
    total = 0.0
    for lo, hi in zip(edges, edges[1:]):
        chunk = pairs[lo:hi]
        if chunk:
            conf = sum(c for c, _ in chunk) / len(chunk)
            acc = sum(k for _, k in chunk) / len(chunk)
            total += len(chunk) / n * (conf - acc) ** 2
    return total ** 0.5


def ece(confidence: list, correct: list, n_bins: int = 10) -> float:
    """Expected calibration error over equal-width confidence bins."""
    n = len(confidence)
    total = 0.0
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        chunk = [(c, k) for c, k in zip(confidence, correct)
                 if lo <= c < hi or (b == n_bins - 1 and c == 1.0)]
        if chunk:
            conf = sum(c for c, _ in chunk) / len(chunk)
            acc = sum(k for _, k in chunk) / len(chunk)
            total += len(chunk) / n * abs(conf - acc)
    return total


def brier(confidence: list, correct: list) -> float:
    return sum((c - k) ** 2 for c, k in zip(confidence, correct)) / len(confidence)


def summary(confidence: list, correct: list, *, n_bins: int = 10) -> dict:
    n = len(confidence)
    out = {"n": n, "accuracy": sum(correct) / n, "mean_confidence": sum(confidence) / n,
           "overconfidence": sum(confidence) / n - sum(correct) / n,
           "rms_calib_err": rms_calib_err(confidence, correct, n_bins),
           "ece": ece(confidence, correct, n_bins), "brier": brier(confidence, correct)}
    if n >= 200:
        out["hle_calib_err"] = hle_calib_err(confidence, correct)
    return out


def bootstrap(confidence: list, correct: list, metric, *, draws: int = 2000,
              seed: int = 0) -> tuple[float, float]:
    """95% percentile interval of `metric` over resampled answers."""
    rng = random.Random(seed)
    n = len(confidence)
    values = []
    for _ in range(draws):
        idx = [rng.randrange(n) for _ in range(n)]
        values.append(metric([confidence[i] for i in idx], [correct[i] for i in idx]))
    values.sort()
    return values[int(0.025 * draws)], values[int(0.975 * draws) - 1]


def paired_bootstrap_diff(base: dict, other: dict, metric, *, draws: int = 2000,
                          seed: int = 0) -> tuple[float, float, float]:
    """Difference other - base in `metric`, resampling the shared items together.

    base and other map item id -> (confidence, correct). Returns (diff, lo, hi).
    """
    ids = sorted(set(base) & set(other))
    rng = random.Random(seed)

    def value(sample, which):
        return metric([which[i][0] for i in sample], [which[i][1] for i in sample])

    diff = value(ids, other) - value(ids, base)
    values = []
    for _ in range(draws):
        sample = [ids[rng.randrange(len(ids))] for _ in ids]
        values.append(value(sample, other) - value(sample, base))
    values.sort()
    return diff, values[int(0.025 * draws)], values[int(0.975 * draws) - 1]

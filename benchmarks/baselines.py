"""Published reference scores, so a local run means something without a second arm.

Running a frontier model through the bridge to compare against costs real API calls and
needs a key the harness cannot borrow. It is also unnecessary for HumanEval, which is
reported so widely that a reference point is a lookup rather than an experiment.

Read these as ORIENTATION, not as a head-to-head. They come from vendor and paper
reporting on harnesses that differ from this one in prompt, extraction and retry policy,
and a few points of pass@1 sit inside that difference. What they are good for is telling
"roughly frontier" from "roughly half" - which is the question actually being asked of a
smaller sovereign model.

The more important caveat is saturation. HumanEval is a solved benchmark at the top of the
range: the strongest models cluster within a couple of points of each other and of the
ceiling, so it cannot separate them. It still separates a mid-tier model from a frontier
one, which is why it is here - and it is why the retrieval and injection suites carry more
weight for this deployment than pass@1 does.
"""
from __future__ import annotations

# (label, approximate pass@1, note). Rounded on purpose: a figure quoted to two decimals
# would imply a precision that cross-harness comparison does not have.
HUMANEVAL = [
    ("canonical solutions", 1.00, "the dataset's own answers - a ceiling, measured here"),
    ("frontier tier (2024-2025 flagships)", 0.90, "vendor-reported, clusters near the ceiling"),
    ("strong mid-tier / small frontier", 0.80, "vendor-reported"),
    ("competent open-weight ~7-14B", 0.60, "commonly reported range"),
    ("older / smaller open-weight", 0.35, "commonly reported range"),
]

# Nothing comparable is published for these: the retrieval questions are written against
# this repository, and the injection cases are written against this bridge's stated
# position on file content being data. They are absolute measures, tracked over time.
LOCAL_ONLY = {
    "retrieval": "no published baseline - ground truth is this repository",
    "injection": "no published baseline - cases target this bridge's own threat model",
}


# Above this, a HumanEval score says more about the dataset than about the model.
# HumanEval was published in 2021 and has been scraped into essentially every training
# corpus since. A mid-tier model scoring at or above the frontier tier on it is the
# expected signature of contamination, not evidence of frontier capability - and quoting
# it as capability is how benchmark numbers stop meaning anything.
_IMPLAUSIBLE = 0.93


def bracket(score: float) -> str:
    """Where a measured pass@1 sits, and a warning when it sits too high to believe."""
    if score >= _IMPLAUSIBLE:
        return (f"{score:.3f} - ABOVE the frontier reference. Read this as a CONTAMINATION "
                f"SIGNAL, not as capability: HumanEval is from 2021 and is in every "
                f"training corpus. Do not quote it as a capability result. The suites that "
                f"still discriminate here are retrieval and injection, whose cases were "
                f"written after the fact and cannot have been trained on.")
    for label, value, _note in HUMANEVAL:
        if score >= value - 0.05:
            return label
    return "below the referenced range"


def table() -> str:
    lines = ["  published reference (orientation only - harnesses differ):"]
    for label, value, note in HUMANEVAL:
        lines.append(f"    {value:>5.2f}  {label:38} {note}")
    return "\n".join(lines)

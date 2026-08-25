"""Compensation applied to Agentaus turns only.

Agentaus is a general-purpose assistant with a 131k window, driven here by a system
prompt written for a much stronger agentic model. The gap shows up in specific,
repeatable ways rather than as vague "worse output":

* It re-calls a tool it has already run, having lost track of what it did.
* It answers from a guess about an API rather than reading the file in front of it.
* It produces code that handles the happy path and ignores the edges.
* It starts editing before working out what the change actually requires.

Each of those is addressable with instruction, so the bridge supplements the system
prompt for Agentaus turns and can optionally have the model review its own answer
before it is returned.

None of this touches Claude models. Those are forwarded byte-for-byte and keep default
Claude Code behaviour exactly - the compensation exists because of a specific gap, and
applying it where there is no gap would only add latency and noise.
"""

from __future__ import annotations

# Appended to Claude Code's own system prompt, not a replacement for it. Kept short:
# every token here is one less available for the conversation, and a long list of
# instructions is itself something a smaller model handles badly.
AGENTAUS_GUIDANCE = """\

--- Operating notes for this model ---

Work in order: understand, plan, act, verify.

1. Before editing anything, read the relevant code. Never guess at an API, a file path, \
a function signature or a flag - open it and look. If you have not read it, say so \
rather than assuming.

2. Track what you have already done. Do not re-run a tool you have already run in this \
conversation unless the inputs have genuinely changed; re-read your earlier tool \
results instead. Repeating a call you have already made wastes a turn and loses \
context.

3. For anything beyond a one-line change, state the plan in two or three lines first, \
then carry it out. If a task has several parts, finish each one before starting the \
next rather than partially doing all of them.

4. When writing code: handle the empty, zero, negative, missing and duplicate cases, \
not only the obvious path. Match the conventions already in the file. Prefer the \
smallest change that fully solves the problem.

5. Before you finish, re-read what you produced and check it against what was asked. \
State any assumption you had to make. If something is incomplete or you are unsure, \
say which part and why - an accurate account of a partial result is worth far more \
than a confident wrong one.

6. Be concise. No preamble, no restating the question, no summary of what you just did \
unless it was asked for.
"""

# Used for the optional review pass. Asking "what is wrong with this" is a markedly
# easier question for a smaller model than getting it right first time, which is what
# makes a second pass worth its latency.
REVIEW_INSTRUCTION = """\
Review the ANSWER below against the REQUEST. You are looking only for real defects:

- Code that is wrong, will not run, or mishandles an edge case (empty, zero, negative, \
missing, duplicate, unicode, very large input)
- Claims about APIs, files or behaviour that were not verified
- Parts of the request that were not addressed
- Internal contradictions

Do not comment on style, naming or formatting. Do not suggest improvements to code \
that is already correct.

If the answer is sound, reply with exactly: OK

Otherwise list the defects, one per line, each with the specific fix.

REQUEST:
{request}

ANSWER:
{answer}
"""

REVISE_INSTRUCTION = """\
Your previous answer to the request below was reviewed and these defects were found. \
Produce a corrected answer.

Output only the corrected answer itself - no preamble, no explanation of what you \
changed, no mention of the review.

REQUEST:
{request}

YOUR PREVIOUS ANSWER:
{answer}

DEFECTS FOUND:
{defects}
"""


def with_guidance(system) -> object:
    """Append the operating notes to whatever system prompt is being sent."""
    if system is None:
        return AGENTAUS_GUIDANCE.strip()
    if isinstance(system, str):
        return system + "\n" + AGENTAUS_GUIDANCE
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": AGENTAUS_GUIDANCE.strip()}]
    return system


def review_says_ok(review: str) -> bool:
    """Whether the reviewer found nothing worth changing.

    Deliberately generous about what counts as approval: a model asked to reply "OK"
    will pad it. Treating a padded approval as a defect list would trigger a pointless
    rewrite of a correct answer, which is worse than skipping a marginal fix.
    """
    if not review:
        return True
    cleaned = review.strip().strip("`*_ \t\n.").upper()
    if cleaned in {"OK", "OK.", "LGTM", "NONE", "NO DEFECTS", "NO ISSUES"}:
        return True
    return cleaned.startswith("OK") and len(cleaned) < 40


def worth_reviewing(text: str, *, min_chars: int = 200) -> bool:
    """Whether an answer justifies a review pass.

    Short answers are usually acknowledgements or one-line facts, where a review costs
    a round trip and finds nothing.
    """
    return bool(text) and len(text.strip()) >= min_chars

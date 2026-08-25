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
# Split in two, and the tool half is only sent when the request actually carries tools.
# A pure code-generation turn given instructions about not re-calling tools spends
# tokens on advice it cannot use, and dilutes the parts that matter - which costs more
# on a smaller model than on a strong one.

CORE_GUIDANCE = """\

--- Operating notes ---

Before writing any code, work out what the problem actually requires:

1. Restate the requirement to yourself in one line, including anything implied but not
spelled out - what should happen on empty input, zero, negatives, duplicates, a single
element, the smallest and largest allowed values, and invalid input that should raise.

2. Write the code so every one of those cases is handled, not just the obvious path.
Sorting, boundary comparisons (< versus <=) and the empty case are where this usually
goes wrong.

3. Re-read what you wrote and trace it against each case you listed. Fix what does not
hold up. Check you have implemented what was asked rather than something adjacent.

4. Prefer the simplest solution that fully works. Match the conventions of any code you
were shown.

5. State any assumption you had to make. If part of it is incomplete or you are unsure,
say which part - an accurate account of a partial result is worth far more than a
confident wrong one.

6. Be concise: no preamble, no restating the question, no summary afterwards unless it
was asked for. When asked for only code, output only code.
"""

TOOL_GUIDANCE = """\

--- Working with tools ---

7. Read before you edit. Never guess at an API, file path, function signature or flag -
open it and look. If you have not read it, say so rather than assuming.

8. Track what you have already done. Do not re-run a tool you have already run in this
conversation unless the inputs genuinely changed; re-read the earlier result instead.
Repeating a call you have already made wastes the turn and loses context.

9. For anything beyond a one-line change, state the plan in two or three lines, then
carry it out. Finish each part before starting the next.
"""

# Kept for callers that want everything regardless of context.
AGENTAUS_GUIDANCE = CORE_GUIDANCE + TOOL_GUIDANCE


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


def guidance_for(body: dict) -> str:
    """The notes that apply to this request.

    Tool discipline is included only when the request actually offers tools. Sending it
    to a plain code-generation turn wastes tokens on unusable advice and dilutes the
    parts that do apply.
    """
    if body.get("tools"):
        return CORE_GUIDANCE + TOOL_GUIDANCE
    return CORE_GUIDANCE


def with_guidance(system, body: dict | None = None) -> object:
    """Append the applicable operating notes to the system prompt being sent."""
    notes = guidance_for(body or {})
    if system is None:
        return notes.strip()
    if isinstance(system, str):
        return system + "\n" + notes
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": notes.strip()}]
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

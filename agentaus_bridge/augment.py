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

0. Find out rather than assume. If a fact is knowable - what a file contains, what a
function signature is, what a command returns, what a document says - get it and read
it. Every assumption you make instead is a place the answer can be confidently wrong.
When you genuinely cannot check something, say so explicitly rather than filling the
gap with a plausible guess.

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

6a. Write in Markdown. Use headings, lists and tables where they make the answer easier
to read, and put every code sample in a fenced block tagged with its language. This does
not override 6: when only code was asked for, output only the code block itself.
"""

TOOL_GUIDANCE = """\

--- Working with tools ---

7. Read before you edit. Never guess at an API, file path, function signature or flag -
open it and look. If you have not read it, say so rather than assuming.

7a. When you need information out of a document, a log, a config file or command
output, read it and interpret it properly. Do not pattern-match a fragment and assume
the rest - a regex over something you have not understood will look right on the case
you tried and be wrong on the next one.

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

Begin your reply with one of these two lines exactly:

VERDICT: OK
VERDICT: DEFECTS

If DEFECTS, list them below that line, one per line, each with the specific fix.
Say OK when the answer is sound - do not invent a defect to seem thorough.

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


VERDICT_LINE = "VERDICT:"

ADJUDICATE_INSTRUCTION = """\
Does the review below report any actual defect that needs fixing?

Answer with exactly one word: YES or NO.

REVIEW:
{review}
"""


def declared_verdict(review: str) -> bool | None:
    """Read the verdict the reviewer was asked to state.

    Returns True for "sound", False for "has defects", or None when the reviewer did
    not follow the format - in which case the caller asks the model rather than
    guessing from the prose, because sniffing for the word "OK" misreads both
    "OK, but the empty case is broken" and a bare approval wrapped in markdown.
    """
    if not review:
        return True
    for line in review.strip().splitlines()[:3]:
        stripped = line.strip().strip("`*_ \t")
        if stripped.upper().startswith(VERDICT_LINE):
            value = stripped[len(VERDICT_LINE):].strip().strip("`*_ .").upper()
            if value.startswith("OK"):
                return True
            if value.startswith("DEFECT"):
                return False
    return None


def review_says_ok(review: str) -> bool:
    """Deterministic reading of a review verdict.

    Kept for the case where no model call is available. `declared_verdict` is the
    primary path; this is the last-resort fallback.
    """
    verdict = declared_verdict(review)
    if verdict is not None:
        return verdict
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


# --------------------------------------------------------------------------------------
# Synthesised thinking
# --------------------------------------------------------------------------------------

# Claude plans inside a thinking block before it acts. Agentaus has no such mode, so it
# answers from the first thing that comes to mind - which is where "started editing
# before working out what the change required" comes from. The bridge gives it the same
# affordance by asking for the plan as its own turn, then handing that plan back as
# context for the real one. Two cheap passes beat one expensive one on a smaller model,
# the same reason the review pass exists.
PLAN_INSTRUCTION = """\
You are about to answer the request below. Before you do, plan the turn.

Work out, briefly:
- What is actually being asked, including anything implied but not stated.
- What you need to find out before you can answer, and which tool would tell you.
- The order of steps, and what "done" looks like.
- Anything you are unsure of, which you must check rather than assume.

Be terse - short lines, no prose, no preamble. This is your own working, not the answer.
Do not answer the request here, and do not write any code.

{tools}REQUEST:
{request}
"""


def should_think(body: dict) -> bool:
    """Whether this turn earns a planning pass.

    Two triggers. An explicit `thinking` block means the user turned extended thinking
    on in the client, and expects the model to think. Tools present means the turn is
    agentic - it will act on the world, and acting without a plan is the failure this
    exists to prevent. A bare prose turn gets neither, because a planning round trip on
    "what does this function do" costs latency and buys nothing.
    """
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        return True
    return bool(body.get("tools"))


def plan_prompt(request: str, body: dict | None = None) -> str:
    """The planning prompt for this turn, naming the tools that are actually available."""
    names = [
        tool.get("name")
        for tool in (body or {}).get("tools") or []
        if isinstance(tool, dict) and tool.get("name")
    ]
    tools = f"TOOLS AVAILABLE: {', '.join(names)}\n\n" if names else ""
    return PLAN_INSTRUCTION.format(tools=tools, request=request[:12000])


def with_plan(system, plan: str) -> object:
    """Fold the plan into the system prompt for the answer call.

    In the system prompt rather than as an assistant message: injecting a synthetic turn
    risks two assistant messages in a row, and the plan is guidance about how to answer
    rather than part of the conversation - the same reasoning that puts the compaction
    summary here.
    """
    if not plan or not plan.strip():
        return system
    notice = (
        "\n\n[Your plan for this turn, which you wrote just now. Follow it, and revise "
        "it if what you find contradicts it.]\n" + plan.strip()
    )
    if system is None:
        return notice.strip()
    if isinstance(system, str):
        return system + notice
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": notice.strip()}]
    return system

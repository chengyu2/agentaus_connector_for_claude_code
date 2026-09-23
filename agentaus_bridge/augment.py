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

import re
import textwrap

from . import prompt_style
from .prompt_style import Template

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

You have real tools and they really run. A tool result in this conversation is output
from your own call, not something the user pasted. Never say you cannot access files,
the filesystem, or a repository - if you need something, call the tool. Never ask the
user to paste a file you can read yourself.
8. Track what you have already done. Do not re-run a tool you have already run in this
conversation unless the inputs genuinely changed; re-read the earlier result instead.
Repeating a call you have already made wastes the turn and loses context.

9. For anything beyond a one-line change, state the plan in two or three lines, then
carry it out. Finish each part before starting the next.

10. Run what you write before you call it done. After writing or changing code, execute
it with Bash against the examples in the task and the edge cases you listed in step 1,
and read the output. If anything fails, fix it and run it again. A change you have not
run is a guess.

11. Finish by doing, not describing. If the task says to write a file, the file must be
written with Write; if it says to run something, run it. Putting the content in your
reply instead leaves the task undone.
"""

# Kept for callers that want everything regardless of context.
AGENTAUS_GUIDANCE = CORE_GUIDANCE + TOOL_GUIDANCE


# Used for the optional review pass. Asking "what is wrong with this" is a markedly
# easier question for a smaller model than getting it right first time, which is what
# makes a second pass worth its latency.
REVIEW_INSTRUCTION = Template("""\
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

<request>
{request}
</request>

<answer>
{answer}
</answer>
""")

REVISE_INSTRUCTION = Template("""\
Your previous answer to the request below was reviewed and these defects were found. \
Produce a corrected answer.

Output only the corrected answer itself - no preamble, no explanation of what you \
changed, no mention of the review.

<request>
{request}
</request>

<your_previous_answer>
{answer}
</your_previous_answer>

<defects_found>
{defects}
</defects_found>
""")


# When to reach for each tool the bridge itself provides. Keyed by tool name so the
# advice can only ever describe a tool that is actually on the wire.
_WHEN_TO_USE = {
    "agentaus_search": (
        "DEFAULT for finding anything. Any question about how something works, where a "
        "behaviour lives, what handles a case, or why a value is set. Ask in plain "
        "language and pass the working directory as `path`."
    ),
    "agentaus_zoom": (
        "Open a citation from a search result and read it in its section. Do this BEFORE "
        "writing from evidence: a search hit proves a fact exists, it does not give you "
        "enough to paraphrase it or to see what it depends on."
    ),
    "agentaus_investigate": (
        "The same as agentaus_search, but from three independent angles, reporting only "
        "what two of them agreed on. Slower. Use it when being wrong would be expensive."
    ),
    "agentaus_web_search": "Anything outside this repository.",
    "Grep": "ONLY an exact literal string you can already spell.",
    "Bash": (
        "Running things - tests, builds, git, one command with bounded output. NOT for "
        "searching: `find`, `ls -R` and `grep -r` produce more than this conversation "
        "can carry, so you are handed a truncated preview and answer from a fragment."
    ),
    "Glob": (
        "Finding files BY NAME. It cannot see inside a file, so it can never answer a "
        "question about content. Do not use it to hunt for a directory whose path you "
        "were already given."
    ),
}


def tool_selection(body: dict) -> str:
    """A `<tool_selection>` block describing only the tools actually on the wire.

    Generated, never hardcoded. A static list drifts the moment a feature is switched
    off: with AGENTAUS_ZOOM=false the model would still be told to call
    `agentaus_zoom`, invent it, and spend a correction round being told it does not
    exist - the bridge teaching its own upstream a tool that the bridge removed.
    """
    names = [
        tool["name"]
        for tool in body.get("tools") or []
        if isinstance(tool, dict) and tool.get("name")
    ]
    described = [(n, _WHEN_TO_USE[n]) for n in names if n in _WHEN_TO_USE]
    if not described:
        return ""

    closing = (
        "If you planned to use a tool, use THAT tool. Do not substitute the one you are "
        "more\nfamiliar with. Listing directories to find your bearings is not progress "
        "- if you\nwere given a path, use it. Every name above is exact; do not invent "
        "others."
    )
    if prompt_style.markdown():
        # A real list: one bullet per tool, rather than columns aligned with spaces,
        # which Markdown collapses.
        lines = [f"- `{name}` - {advice}" for name, advice in described]
        return ("\n" + prompt_style.section(
            "tool_selection", "Finding things:\n\n" + "\n".join(lines) + "\n\n" + closing
        ) + "\n")

    width = max(len(n) for n, _ in described) + 2
    lines = []
    for name, advice in described:
        wrapped = textwrap.wrap(advice, width=76 - width)
        head = f"  `{name}`".ljust(width + 2)
        lines.append(f"{head} - {wrapped[0]}")
        lines.extend(" " * (width + 5) + part for part in wrapped[1:])

    return (
        "\n<tool_selection>\nFinding things:\n"
        + "\n".join(lines)
        + "\n\n" + closing + "\n</tool_selection>\n"
    )


def guidance_for(body: dict) -> str:
    """The notes that apply to this request.

    Tool discipline is included only when the request actually offers tools. Sending it
    to a plain code-generation turn wastes tokens on unusable advice and dilutes the
    parts that do apply.
    """
    if body.get("tools"):
        return (prompt_style.headings(CORE_GUIDANCE + TOOL_GUIDANCE)
                + tool_selection(body))
    return prompt_style.headings(CORE_GUIDANCE)


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

ADJUDICATE_INSTRUCTION = Template("""\
Does the review below report any actual defect that needs fixing?

Answer with exactly one word: YES or NO.

<review>
{review}
</review>
""")


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


# Phrasings Agentaus uses when it declines to use tools it was given. It is not
# refusing on policy grounds - it is asserting, wrongly, that it has no filesystem. The
# tools were on the wire and the working directory was in the prompt.
# Whether an answer is a refusal is a judgement about language, not a pattern. Three
# phrase lists used to live here - roughly forty entries - and they were wrong in both
# directions: they missed phrasings nobody had thought of, and they needed a separate
# list of "filesystem words" beside them to stop "I don't have access to next year's
# budget" being read as a refusal to use tools.
#
# Agentaus decides now. The trigger for asking is STRUCTURAL and costs nothing: a turn
# that was offered tools, called none of them, and produced a short answer. Only then is
# there anything to classify, and only then is a call worth making.

# Replies up to this long are judged. It was 1,200, on the theory that a long reply is an
# answer - and the benchmark found the long failure that theory misses: asked to write
# and run pareto.R, the model printed the whole script in its reply and stopped. With
# the request in front of it the judge tells that apart from a real long answer, so the
# ceiling now only bounds the prompt.
_REFUSAL_LENGTH_CEILING = 20000

CLASSIFY_REFUSAL_INSTRUCTION = Template("""\
An AI agent was given tools that act on the local machine - reading and writing files,
running commands - and the task below. Its turn ended with this reply, and it called no
tool.

<request>
{request}
</request>

<tools_it_ran>
{ran}
</tools_it_ran>

<reply>
{answer}
</reply>

<question>
Which one is the reply? Judge against the request and what the agent already ran: if
the tools it ran have already done what the request asks - the file written, the command
run - a reply saying so is an ANSWER, however short.

REFUSAL - the agent declines to act: it claims it cannot read files or has no access to
the filesystem, or asks the human to paste, upload or run something it could have done
itself with its tools.

STALLED - the agent stopped before the task was done, and nothing it needs from the
human is missing. Either it says what it will do next without doing it ("We will write
the file now", "Next I'll run the tests", "We need to write answers.json"), or its reply
is a fragment of its own reasoning, or it put the deliverable in the reply instead of
doing what the request asked for with it - printing a file's contents when the request
said to write the file, showing a command when the request said to run it.

ANSWER - a genuine final answer or result, or a legitimate statement about something it
really cannot know or do, or a question the human genuinely has to answer.
</question>

<output_format>
Exactly one word: REFUSAL, STALLED or ANSWER. Nothing else.
</output_format>
""")



def could_be_a_refusal(answer: str, *, tools_offered: bool, called_a_tool: bool) -> bool:
    """Whether this turn is even worth classifying - structural, no language involved.

    A cheap gate in front of the model call. A turn that used a tool is acting; a long
    answer is answering; a turn offered no tools cannot be refusing to use them.
    """
    if called_a_tool or not tools_offered:
        return False
    text = (answer or "").strip()
    return 0 < len(text) <= _REFUSAL_LENGTH_CEILING


def read_turn_verdict(verdict: str) -> str:
    """"refusal", "stalled" or "answer". Anything unrecognised is an answer, for the same
    reason as `read_refusal_verdict`: re-asking a good answer is the worse mistake."""
    word = (verdict or "").strip().upper().lstrip("*`_ ")
    if word.startswith("REFUSAL"):
        return "refusal"
    if word.startswith("STALLED"):
        return "stalled"
    return "answer"


def read_refusal_verdict(verdict: str) -> bool:
    """Read the one-word answer. Anything unrecognised is treated as a real answer.

    Deliberately biased that way: wrongly re-asking a good answer wastes a round trip
    and confuses the model, while wrongly accepting a refusal costs one turn the user
    can simply repeat.
    """
    return (verdict or "").strip().upper().startswith("REFUSAL")


REFUSAL_CORRECTION = Template("""\
<correction>
That is not true, and it was not the question.

You DO have tools, they are listed in your system prompt, and they run on this machine
right now. `agentaus_search` and `agentaus_zoom` read the local filesystem directly. The
absolute path you were given is real and readable.

Nobody is going to paste or upload anything for you. Call the tool.

Start over and follow the task exactly as it was given.
</correction>
""")


# The small-model failure the refusal check was not built for: the turn ends on an
# announcement. Observed on the benchmark pilot - after one correction round, the reply
# was "We will write file at same directory." and nothing was written, so the session
# ended with the task undone.
STALLED_CORRECTION = Template("""\
<correction>
You stopped before the task the user gave you was done. Saying what you will do, or
showing what a file should contain, does not do it - nothing was written or run.

Do the remaining step of THAT task now: call the tool. Write only the files the user's
task asks for, and run only what it needs. Do not write notes, memories, summaries or
anything else the task did not ask for.

If the task really is complete, reply with the final result instead.
</correction>
""")


GROUNDING_INSTRUCTION = Template("""\
An agent answered a question after using tools. Below is what it actually did, and what
it then said.

<tools_it_actually_ran>
{ledger}
</tools_it_actually_ran>

<its_answer>
{answer}
</its_answer>

<task>
Find statements in the answer that the agent could not possibly know from what it ran.

Look for:
- claims about the CONTENTS of a file it never read
- comparisons against material it never opened
- named policies, rules, conventions or standards that appear nowhere in what it ran
- numbers or counts that contradict each other, or that nothing it ran could produce

A statement is fine if it follows from a tool result, is general knowledge, or is clearly
offered as a suggestion rather than a finding.
</task>

<output_format>
If everything checks out, reply with exactly: GROUNDED

Otherwise reply with GAPS on the first line, then one line per unsupported statement:
the claim, then what would have been needed to make it. Nothing else.
</output_format>
""")


STRIP_UNGROUNDED_INSTRUCTION = Template("""\
Your answer below contains statements you had no basis for. They are listed after it.

<your_answer>
{answer}
</your_answer>

<unsupported>
{gaps}
</unsupported>

<task>
Reissue the answer with each unsupported statement either removed, or rewritten as the
open question it actually is - "the repo may contain X; I did not read it" rather than a
claim about X.

Change nothing else. Keep every grounded statement, its wording, and the structure and
formatting of the original. Do not add new material, do not apologise, and do not mention
this correction.
</task>
""")


def grounding_verdict(reply: str) -> str:
    """The listed gaps, or "" when the answer was found grounded.

    Anything unparseable counts as grounded: a check that cannot be read must not be
    allowed to rewrite an answer, because rewriting a good answer is the more expensive
    mistake.
    """
    text = (reply or "").strip()
    if not text or text.upper().startswith("GROUNDED"):
        return ""
    if not text.upper().startswith("GAPS"):
        return ""
    return "\n".join(text.splitlines()[1:]).strip()


def worth_grounding_check(body: dict, answer: str, *, min_chars: int = 400) -> bool:
    """Whether this turn's answer should be checked against what the turn actually did.

    The opposite condition to `worth_reviewing_turn`: a turn that used tools is exactly
    the turn where a claim can outrun the evidence, and it is the turn ordinary review
    has to sit out because a reviewer cannot see tool results.

    Observed on a real session: asked to survey a repository, the model ran one `find`,
    saw a 2 KB preview of a 1.6 MB listing, and then wrote that "the repo's `_notes.md`
    usually enforces a single consistent fit label" and that no answer breached "the
    prohibitions live once rule" - a file it never opened and a policy that does not
    exist. Nothing caught it, because review is disabled on exactly these turns.
    """
    if not mid_tool_loop(body) and not _ran_a_tool(body):
        return False
    return len((answer or "").strip()) >= min_chars


def _ran_a_tool(body: dict) -> bool:
    for message in body.get("messages") or []:
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
    return False


def worth_reviewing_turn(body: dict) -> bool:
    """Whether this turn's answer can be fairly reviewed at all.

    The reviewer is a fresh call given the request and the answer - and nothing else.
    When the answer was derived from a tool result, the reviewer cannot see that result,
    so it judges a well-grounded answer as an unverified claim and rewrites it into a
    refusal.

    Observed live, and it cost hours to find because every component was individually
    correct: Agentaus read a 60,000-character tender document and correctly listed the
    Section 5 headings, then the review pass - shown only "list the Section 5 headings"
    and a list of headings - concluded the document had never been provided and replaced
    the answer with "Please provide the DOCX file". The bridge was reliably turning a
    right answer into a wrong one.
    """
    return not mid_tool_loop(body)


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
PLAN_INSTRUCTION = Template("""\
You are an agent working inside a code repository. Plan the turn below before you act.

This is your own private working. Nobody reads it and nobody answers it, so a plan that \
asks a question is a plan that does nothing. Everything you need is either already \
here or reachable with the tools listed - go and get it.

{context}<task>
Work out, briefly:
- What is actually being asked, including anything implied but not stated.
- What you must find out first, and WHICH LISTED TOOL will tell you. Name the tool \
exactly as it is written above and give the arguments you will pass it.
- The order of steps, and what "done" looks like.
- Anything you are unsure of, which you must check rather than assume.
</task>

<rules>
- Use ONLY the tools listed above. Do not invent a tool name. If nothing listed can get \
you a fact, say so and plan around it.
- Choose the tool by what its description says it is for, not by which name you \
recognise. If a tool exists for searching by meaning, prefer it over a regex search for \
any question about how something works or where a behaviour lives - a regex only finds \
text you can already spell.
- Never plan to ask the user for something you can look up yourself - especially not \
where the code is. You already know.
- Be terse: short lines, no prose, no preamble.
- Do not answer the request here, and do not write code here.
</rules>

<request>
{request}
</request>
""")


def mid_tool_loop(body: dict) -> bool:
    """Whether the conversation is waiting on the model to use a tool result it just got.

    Two passes must sit out when this is true, for the same reason: neither the planner
    nor the reviewer can see tool results, so both judge the turn on the user's original
    message alone and reach a conclusion the evidence contradicts.
    """
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        return isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def should_think(body: dict) -> bool:
    """Whether this turn earns a planning pass.

    Two triggers. An explicit `thinking` block means the user turned extended thinking
    on in the client, and expects the model to think. Tools present means the turn is
    agentic - it will act on the world, and acting without a plan is the failure this
    exists to prevent. A bare prose turn gets neither, because a planning round trip on
    "what does this function do" costs latency and buys nothing.

    But NOT when a tool result has just come back. The plan is written from the user's
    message, and a planner cannot see tool results - so mid-loop it re-plans the step
    already taken and tells the model to run the tool it just ran. Observed live: the
    model read a document, was handed a fresh plan saying "read the document", and
    replied "I am unable to access files on your local system" while holding the file's
    contents. The plan from the first step is already in the conversation; re-deriving
    a worse one is not just wasted latency, it is actively misleading.
    """
    if mid_tool_loop(body):
        return False
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        return True
    return bool(body.get("tools"))


# Claude Code states the working directory in its system prompt, in one of a few
# phrasings. The planning call is a *fresh* conversation - it does not inherit that
# prompt - so without lifting the path out explicitly the planner has no idea where the
# code is. Observed live: it planned to "ask user for the codebase source (repo URL,
# internal path, or name)" while the answer was sitting in the system prompt all along.
_CWD_PATTERNS = (
    re.compile(r"(?:primary\s+)?working\s+directory[^\n:]*:\s*(\S+)", re.I),
    re.compile(r"\bcwd\b[^\n:]*:\s*(\S+)", re.I),
    re.compile(r"current\s+(?:working\s+)?dir(?:ectory)?[^\n:]*:\s*(\S+)", re.I),
)


def working_directory_of(body: dict) -> str | None:
    """The working directory, wherever this client version put it.

    Claude Code 2.1.278 no longer states it in `system`: the environment block arrives as
    a `system`-role entry in `messages`. Reading `system` alone found nothing, so the
    planner, search path defaults and relative-path repair all silently lost the repo.
    """
    from .translate import system_notes
    found = working_directory((body or {}).get("system"))
    if found:
        return found
    return working_directory("\n".join(system_notes(body or {})))


def working_directory(system) -> str | None:
    """The repository path Claude Code named in its system prompt, if it named one."""
    if isinstance(system, list):
        text = "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict) and b.get("text")
        )
    elif isinstance(system, str):
        text = system
    else:
        return None
    for pattern in _CWD_PATTERNS:
        found = pattern.search(text)
        if found:
            path = found.group(1).strip().strip("`'\".,")
            if path.startswith("/") or path.startswith("~"):
                return path
    return None


_TOOL_BLURB_CHARS = 240


def _one_line(description) -> str:
    """The first sentence or two of a tool description, on one line.

    Claude Code's descriptions run to paragraphs, and a planning prompt carrying all of
    them would cost more window than the plan is worth. The opening is where each one
    says what it is for, which is the only part the planner needs.
    """
    text = " ".join(str(description or "").split())
    if len(text) <= _TOOL_BLURB_CHARS:
        return text
    cut = text[:_TOOL_BLURB_CHARS]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: stop + 1] if stop > 80 else cut).rstrip() + " …"


def plan_prompt(request: str, body: dict | None = None) -> str:
    """The planning prompt for this turn, naming the tools that are actually available.

    The tool list is not decoration: a planner shown no tools plans to ask questions,
    and a planner shown the wrong ones invents names. Both were observed live.
    """
    body = body or {}
    lines = []
    cwd = working_directory_of(body)
    if cwd:
        where = (f"## Working directory\n\n`{cwd}`\n\n" if prompt_style.markdown()
                 else f"<working_directory>\n{cwd}\n</working_directory>\n")
        lines.append(
            where + "That is the repository. Pass it as the `path` argument to any tool "
            "needing one."
        )

    # Names alone are not enough. Given a bare list, the planner picks the tool it knows
    # from training - which is `Grep` - and never reaches for the better one it has
    # never heard of. Observed live: the plan chose a regex over `agentaus_search` for a
    # conceptual question, which is the exact failure this whole feature exists to stop.
    entries = []
    for tool in body.get("tools") or []:
        if not (isinstance(tool, dict) and tool.get("name")):
            continue
        if prompt_style.markdown():
            entries.append(f"- `{tool['name']}` - {_one_line(tool.get('description'))}")
        else:
            entries.append(
                f"  <tool name=\"{tool['name']}\">{_one_line(tool.get('description'))}</tool>")
    if entries:
        if prompt_style.markdown():
            lines.append("## Tools available\n\n" + "\n".join(entries)
                         + "\n\nUse these exact names and no others.")
        else:
            lines.append("<tools_available>\n" + "\n".join(entries)
                         + "\n</tools_available>\nUse these exact names and no others.")

    context = ("\n\n".join(lines) + "\n\n") if lines else ""
    return PLAN_INSTRUCTION.format(context=context, request=request[:12000])


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


# --------------------------------------------------------------------------------------
# Tool focus
# --------------------------------------------------------------------------------------

# What a coding agent needs on every turn. Measured: Claude Code 2.1.278 sends 26 tools
# carrying 120 KB of schemas - about 30,000 tokens a call, a quarter of Agentaus'
# window - and the largest are Artifact (34 KB), ArtifactData, DesignSync and Monitor,
# none of which a coding turn uses. A strong model shrugs that off. A smaller one pays
# for it twice: in window, and in choosing among 26 names, which is where the invented
# and misspelt calls come from.
CORE_TOOLS = {
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput",
    "KillShell", "KillBash", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "TodoWrite",
    "TodoRead", "Skill", "Agent", "Task", "AskUserQuestion", "ToolSearch",
    "EnterPlanMode", "ExitPlanMode",
}


def _tools_used(body: dict) -> set:
    used = set()
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                    used.add(block["name"])
    return used


def _last_user_words(body: dict) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(t.strip() for t in texts):
                return "\n".join(texts)
    return ""


def focus_tools(body: dict) -> tuple[dict, list]:
    """Offer the coding tools, plus any tool the user names or the conversation used.

    Nothing the user asks for goes missing: a tool named in their message, or already
    called in this conversation, stays on the wire. Returns (body, dropped names).
    """
    tools = body.get("tools") or []
    if not tools:
        return body, []
    asked = _last_user_words(body)
    used = _tools_used(body)
    kept, dropped = [], []
    for tool in tools:
        name = (tool or {}).get("name") or ""
        server = name.split("__")[1] if name.startswith("mcp__") and "__" in name[5:] else ""
        if (name in CORE_TOOLS or name in used
                or (name and re.search(rf"\b{re.escape(name)}\b", asked))
                or (server and re.search(rf"\b{re.escape(server)}\b", asked, re.I))):
            kept.append(tool)
        else:
            dropped.append(name)
    if not dropped:
        return body, []
    return {**body, "tools": kept}, dropped


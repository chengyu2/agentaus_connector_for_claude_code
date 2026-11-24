# Skills: the know-how, not the capability

A **tool** does something — a round trip that returns data. A **skill** tells the model
*how* — Markdown loaded into context that returns behaviour. The bridge ships tools; the
skills in `.claude/skills/` ship the procedures, and Claude Code loads one when its
description matches what you asked for.

They matter more here than they would with a stronger model. Most of the disappointing
answers this bridge produced were not capability gaps — they were a competent model taking
the wrong approach, and an approach is exactly what a skill supplies.

| Skill | The pain point it encodes |
| --- | --- |
| `repo-survey` | Asked to review a repo, ran one `find`, answered from a 2 KB preview of a 1.6 MB listing, and **invented a policy that does not exist** |
| `find-in-code` | Reached for `Grep` and `find` — which cannot answer *"where do we cap concurrent calls"*, since the answer shares no word with the question |
| `read-documents` | A regex flatten of a `.docx` lost **2,695 characters** across 12 rows and could not see the compliance column |
| `tender-evidence` | Four rows of 43 proposed additions asserting real certifications **with no quote behind them** |
| `bridge-diagnose` | 63 timeouts and 1236 slot waits read as separate faults when they were one |
| `search-exhaustively` | Asked to go through hundreds of documents, answered from **one or two** — because the stopping condition was "I have something to say" rather than "there is nothing left to find" |
| `investigate` | A single pass produces one story about the evidence, and a fluent story is indistinguishable from a correct one once written down |
| `research` | Current facts answered from memory with no source, and web claims blended into repository claims with no way back |

Each names the failure it prevents, with the measurement, because a rule whose reason is
stated is followed more reliably than one asserted. They are deliberately short — every
token of instruction is one less for the conversation, and a long list of rules is itself
something a smaller model handles badly.

## The model has to call `Skill`, and this one does not

Everything above assumes the model loads a skill when its description matches. Claude Code
offers `Skill` as an ordinary tool and expects it to be called. Measured on the same
prompt, in the same directory, with the same skills present and `Skill` among the 28 tools
on the wire:

```
opus      ['Bash', 'Bash', 'Skill', 'Bash', 'Bash', ...]   invoked Skill: True
agentaus  ['Read']                                         invoked Skill: False   (x5)
```

Naming `Skill` first in the generated `<tool_selection>` block did not move it: still
`['Read']`. So the procedures written to compensate for this model were not reaching it,
and the table above described a mechanism that worked on the model that needed it least.

The fix is the one the rest of the bridge already applies here: **do not ask it to choose,
hand it the thing.** A skill whose gate fires is injected into the system prompt as
`<applicable_procedures>`; `Skill` stays on the wire for everything no gate covers.

The gates are structural and cost nothing - a tabular file named anywhere in the turn
selects `analyse-data`, five or more numbered steps selects `multi-step-work`. Both had to
be narrowed after measurement. The first version counted bulleted lines as steps, so a
two-panel chart task listing two panels and two JSON keys totalled four, drew in the
long-task procedure on top of the analysis one, and the run that followed answered *"We
will write the script `generate_rnd_plot.py` in the working directory, then run it"* and
stopped - one turn, no tool calls, nothing on disk, against 6/6 for the same task without
it. Restricting steps to numbered lines and raising the threshold took it back to 6/6.

That is this repository's own rule turned on itself: skills are **deliberately short**,
and a long list of rules is itself something a smaller model handles badly. Injecting two
of them at once is a long list of rules.

Finding the skills directory is done by asking the filesystem, not by parsing the prompt.
The bridge used to read the working directory out of a labelled line in Claude Code's
system prompt; that label is absent in 2.1.278, so the parse returned `None` and every
lookup silently found nothing. Candidate paths are now taken from the request, walked
upward, and the one with a `.claude/skills` directory wins. A label can be renamed; a
directory either has the folder or it does not.

Parsed skills are cached against the skills directory's mtime. This is disk work on the
request path, and the commit that moved search into worker threads established that such
work must not block the event loop - 0.22ms for ten skills, 0.005ms once cached.

## Routing: "if this, use that"

Why the model would not choose was found on Claude Code 2.1.281, which sends a **project**
skill as a bare name - `- find-in-code`, no description - for every model. Opus picks the
right skill from the name alone. Agentaus cannot choose between names it has no purpose
for, and restoring the descriptions inside Claude Code's list did not move it either: the
list is the last thing in a long system prompt, and the planner never saw it at all.

So the bridge now does three things when `Skill` or `Agent` is on the wire
(`AGENTAUS_SKILL_ROUTING`, on by default):

1. **Descriptions go back into the listing**, read from each skill's own front matter.
2. **A "Skills and subagents" section is added to the guidance**: one line per skill and
   agent type, built from the description's own *"Use when ..."* clause:

   ```
   - If a question involves a .docx, .xlsx, .pptx or .pdf ... → `Skill` with `skill: "read-documents"`
   - If answering means sweeping many files ... → `Agent` with `subagent_type: "Explore"`
   - If none of these fits → no skill or subagent; use your tools directly
   ```

   It opens with the budget: **there is no API or token limit on this model**, so loading a
   skill or starting a subagent costs nothing that matters.
3. **The planner gets the same lines and must begin with its choice** - `Skill: <name>` or
   `Skill: none`, and `Agent: <type>` or `Agent: none` - so the decision is made, not
   skipped.

A loaded skill needed one more fix. Claude Code's tool result says only *"Launching skill:
read-documents"* and the procedure follows as a plain message. Agentaus passed a `.docx`
path as the skill's argument, expected the document back, got instructions, did not follow
them, and loaded the same skill three more times. The bridge now heads a loaded skill
*"Procedure to follow now"* and says it is instructions, not a result; a repeat load is told
it is already in the conversation.

## The bridge's own skill library

`agentaus_bridge/skill_library/` holds skills adapted from Anthropic's Apache-2.0
plugins (attribution in its `NOTICE`), rewritten for this model in annotated Markdown:

| Skill | Adapted from |
| --- | --- |
| `thorough-code-review` | `code-review`, and `pr-review-toolkit`'s six review lenses as parallel subagents |
| `feature-development` | `feature-dev`: explore, design, build, review, with subagents per phase |
| `simplify-code` | `code-simplifier` |
| `secure-coding` | `security-guidance`'s dangerous-pattern catalogue, with a scanner script |
| `git-commit-and-pr` | `commit-commands` |
| `math-reasoning` | `math-olympiad`, refocused on everyday quantitative questions |
| `skill-authoring` | `skill-creator` and `plugin-dev`, with a spec validator script |

Claude Code has never heard of them, so the bridge serves them itself
(`AGENTAUS_SKILL_LIBRARY`, on by default). It adds them to the listing, after the project's
own skills and before Claude Code's bundled ones, and runs any `Skill` call that names one
like its own tools. They reach every project, including one with no `.claude/skills`, and
Claude sessions never see them. A project skill with the same name stays the project's.

## Keeping them from doubling up with the tools

A tool is a **capability**; a skill is **when to reach for it and how to know you are
finished**. No skill re-explains what a tool does. `find-in-code` is the router and hands
off; each specialist owns one tool; `search-exhaustively` names no tool at all, because a
stopping criterion is not a capability.

That boundary had to be enforced, not just stated. `find-in-code` carried the rule *"one
search, then at most two zooms, then answer"* — correct for locating one thing, and
precisely the wrong instruction for *"go through the whole repo"*, which is the question
that kept producing two-file answers. One skill was quietly arguing with another. It is
now scoped to single-target lookups and points at `search-exhaustively` for the rest.

Nothing about them is Agentaus-specific. They are ordinary Claude Code skills and work on
any model; they simply matter more where the model needs the script.

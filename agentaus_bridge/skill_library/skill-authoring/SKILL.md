---
name: skill-authoring
description: Write, improve or review an Agent Skill - a SKILL.md with spec-compliant front matter, a trigger-ready description, numbered steps and optional references or scripts - for Claude Code or Agentaus, then prove it helps by running the same task with and without it. Use when asked to create, write, edit, review, validate or test a skill or SKILL.md, turn a workflow or conversation into a skill, or fix a skill that never gets used. Includes a standard-library validator that also prints the routing line Agentaus will see.
license: Apache-2.0
metadata:
  source: claude-plugins-official/plugins/skill-creator/skills/skill-creator; claude-plugins-official/plugins/plugin-dev/skills/skill-development
  adapted-for: agentaus
---

# Writing a skill that gets used, and helps when it is

A skill fails in one of two ways: it never loads, because its description does not match
what users type, or it loads and changes nothing, because its steps are vague. This
procedure writes the description as a trigger, writes the body as steps a smaller model can
follow, and tests both against a run without the skill.

## When to use

- "Make a skill for X", "turn what we just did into a skill", "write a SKILL.md".
- Editing, reviewing or validating an existing skill.
- A skill exists but the model never loads it, or loads it and does no better.
- Not for: slash commands, hooks, subagent definitions or plugin manifests - different formats.

## How a skill is loaded

1. **Metadata** (`name` + `description`) is always visible in the skill listing. The model decides from this alone whether to load the skill.
2. **The body** (everything after the front matter) is loaded when the skill is chosen.
3. **Bundled files** (`references/`, `scripts/`, `assets/`) are opened only when the body points at them. A script can be run without being read.

On Agentaus the bridge turns each description into one routing line:
`If <clause> → Skill with skill: "<name>"`. The clause is the text after the first `Use when`,
up to the first full stop followed by a space, cut at about 230 characters. With no `Use when`
the line degrades to a vague "the task needs: ...". **The `Use when` sentence is the trigger.**

## Steps

1. **Capture intent.** Write down four things: what the skill lets the model do; the phrases and situations that should trigger it; what it must produce; how you will tell it worked. If the conversation already holds the workflow, extract the tools used, the order of steps and the user's corrections from it first. Ask the user only about gaps.
2. **Write 2-3 test prompts now**, before the skill: what a real user would type, with file names, paths and context. Save them in a workspace, for example `<workspace>/evals/prompts.md`.
3. **Check for an existing skill.** `Glob` for `**/SKILL.md` under `.claude/skills/`, `~/.claude/skills/`, and `agentaus_bridge/skill_library/` if you are in the bridge repository. If one overlaps, improve it and keep its `name`.
4. **Choose the name and place.** Name: `a-z`, `0-9` and single hyphens, at most 64 characters, identical to the directory name. Place: see the table below.
5. **Plan bundled files** by walking through each test prompt. Code you would rewrite every time → `scripts/`. Long documentation, schemas, checklists → `references/`. Templates or boilerplate copied into output → `assets/`. Keep each one level deep, and create only the directories you need.
6. **Draft outside the live skills folder** (for example `<workspace>/draft/<name>/`), so a baseline test run cannot load the draft.
7. **Write the front matter** from the template below.
8. **Write the body** from the template below, following "Writing for a smaller model".
9. **Validate.** Run `python3 <this skill's directory>/scripts/validate_skill.py <draft directory>`. Fix every `FAIL`. Read every `WARN` and the printed routing line.
10. **Test with and without the skill** (section below; full protocol in `references/testing.md`).
11. **Improve and re-test** until the stop rule in the testing section is met.
12. **Install.** Copy the directory to its place and run the validator again on the installed copy.

## Where a skill lives

| Place | Who sees it | Size note |
| --- | --- | --- |
| `<project>/.claude/skills/<name>/SKILL.md` | Claude Code in that project; the bridge also injects it into Agentaus's prompt when a gate fires | injected bodies are cut at 9000 characters |
| `~/.claude/skills/<name>/SKILL.md` | Claude Code in every project | - |
| `agentaus_bridge/skill_library/<name>/SKILL.md` | Agentaus in every project, served by the bridge when `Skill` names it | bodies cut at 40000 characters; references opened with `agentaus_zoom` |

The two limits are the bridge's current values (`_MAX_SKILL_CHARS` and `_LIBRARY_MAX_CHARS` in
`agentaus_bridge/skills.py`); check there if they matter.

## Front matter template

```yaml
---
name: csv-cleanup
description: Clean a messy CSV or TSV - fix headers, types, duplicates and encoding - and write a tidy copy with a change log. Use when asked to clean, tidy, fix or normalise a .csv or .tsv file, when a CSV fails to load, or when columns have mixed types or stray header rows.
license: Apache-2.0
metadata:
  author: your-team
---
```

Only `name` and `description` are required. Optional: `license`, `compatibility` (at most
500 characters, for required tools or packages), `metadata` (string keys to string values),
`allowed-tools`. Field details and YAML pitfalls: `references/spec.md`.

## Description rules

1. One line, 1-1024 characters, plain text.
2. First sentence: what the skill does, with concrete nouns - file types, tools, outputs.
3. Second sentence starts exactly `Use when` and lists triggers: phrases users type ("clean up this CSV", "where is"), file types (`.xlsx`, `CLAUDE.md`), situations ("before a refactor", "when a load fails").
4. Keep that sentence under about 230 characters, with no `e.g. ` or `etc. ` - the routing line stops at the first full stop followed by a space.
5. Say `Use when` once, and put no "; when" or "— when" before it: the bridge takes the first match.
6. Be slightly pushy. Models under-load skills; name the adjacent situations where it should still fire.
7. YAML safety: no `: ` (colon then space) and no ` #` in an unquoted description, and do not start it with a quote, `[`, `{`, `*`, `&`, `!`, `|` or `>`. Never use `<` or `>` anywhere in it.
8. plugin-dev's third-person style ("This skill should be used when ...") does not match the bridge's pattern. For Agentaus, write `Use when`.

- Bad: `description: Helps with CSV files.` - no triggers, no output.
- Bad: `description: Use this for data.` - vague, and the trigger is the whole description.
- Good: the template above - what, then `Use when` with phrases, file types and failure situations.

## Body template

````markdown
# [Title: the outcome, not the topic]

[One or two sentences: what goes wrong without this skill.]

## When to use
- [situation, in the words a user would use]
- Not for: [near miss] → [what to use instead]

## Steps
1. [One instruction, imperative, naming the exact tool: `Grep`, `Bash`, `Write` ...]
2. [...]

## If this → do that
- [concrete input] → [concrete action]

## Done when
- [a checkable condition: a file exists, a command passes, a count matches]

## References
- `references/[file].md` - [what is in it]; open it when [condition].
````

## Writing for a smaller model

- One instruction per numbered step, in the imperative ("Run", "Write", "Stop"), naming the exact tool.
- Give the reason in half a line when a step could look optional; a model that knows why skips less. Prefer a reason to all-caps MUST.
- Replace judgement words ("thoroughly", "carefully", "as needed") with a check: a count, a file that must exist, a command that must pass.
- State when to stop, and what to do when stuck ("after two failed fixes, report both results").
- Add worked `If X → do Y` lines with realistic inputs.
- Put templates in fenced blocks, exactly as the output should look.
- Keep the body lean. Facts the model already knows cost context and change nothing. Move detail to `references/` with one line saying when to open each file; give a reference over 300 lines a table of contents. Never say the same thing in both.
- Name only tools that exist where the skill will run.
- Nothing surprising: a skill must do only what its description leads a user to expect. No hidden instructions, data exfiltration or security bypasses.

## Testing with and without the skill

1. For each test prompt, in **one turn**, launch two `Agent` calls with `subagent_type: "general-purpose"`:
   - With the skill: "Read `<draft>/SKILL.md` and follow it to do this task: `<prompt>`. Save all outputs to `<workspace>/iteration-1/<eval-name>/with_skill/`."
   - Baseline: the same task and output folder name `without_skill/`, with no skill path. When improving an existing skill, first copy the old version (`cp -r`) and use it as the baseline, saving to `old_skill/`.
2. While they run, write assertions: statements that can be checked objectively ("`out.csv` has no duplicate rows", "the report cites `path:line` for each claim").
3. Grade every output against every assertion, with a script where possible. A pass needs the right content, not just the right file name. Record the evidence.
4. Compare pass rates, then read the outputs and transcripts: did the skill make the run waste steps or skip something?
5. There is no token limit on Agentaus: run each configuration 3 times when results vary, and compare the spread, not one run.

**Stop rule:** stop when the with-skill runs pass every assertion on every test prompt and beat
the baseline (or match it in fewer steps), in two consecutive iterations. Also stop after two
iterations with no improvement, and report what still fails.

## If this → do that

- "Turn what we just did into a skill" → extract the steps, tools and corrections from the conversation; draft; confirm the trigger phrases with the user before testing.
- The skill never loads on Agentaus → run the validator and read the routing line: add `Use when` if missing, move the users' actual words into the first 230 characters, and remove any `. ` that cuts the clause short.
- The skill loads but results do not improve → read the with-skill transcripts; find the step where the run diverged or wasted calls; make that step concrete, or delete it.
- Every test run wrote the same helper script → put it in `scripts/`, test it, and replace the step with "run `scripts/<file>`".
- A project skill's body is over 9000 characters → move detail to `references/`, keeping a one-line pointer each.
- Updating an existing skill → keep its `name` and directory; snapshot it first for the baseline.
- Asked to check triggering → write the query set in `references/testing.md` and read the description against each query.

## Done when

- The validator reports no `FAIL` for the installed copy.
- The printed routing line reads as something a real request would match.
- With-skill runs beat the baseline on the test prompts, or match it in fewer steps.
- Every file the body mentions exists, and each reference says when to open it.

## References

Open these with `agentaus_zoom` (absolute path under this skill's directory, `start_line` 1) or `Read`.

- `references/spec.md` - every front-matter field, the directory layout, YAML pitfalls, progressive-disclosure sizes, and the bridge's limits. Open it when writing front matter or when the validator fails.
- `references/testing.md` - the with/without protocol in detail: workspace layout, assertions, grading, blind A/B comparison, and trigger-query sets. Open it before step 10.
- `scripts/validate_skill.py` - standard-library validator. Run `python3 scripts/validate_skill.py <skill-dir> [<skill-dir> ...]`; exit status 1 means a `FAIL`.

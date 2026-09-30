# Testing a skill: with/without runs, grading, comparison, triggering

Adapted from skill-creator's evaluation loop. Everything here runs with Claude Code's own
tools (`Agent`, `Write`, `Bash`); nothing calls an external model API or a command-line model.

## Contents

1. Workspace layout
2. Test prompts
3. Running with and without
4. Writing assertions
5. Grading
6. Comparing and deciding what to change
7. Blind A/B comparison
8. Trigger-query sets

## 1. Workspace layout

Keep test material beside the draft, never inside the live skills folder:

```
<name>-workspace/
├── draft/<name>/             the skill being tested
├── skill-snapshot/           copy of the old version (when improving an existing skill)
├── evals/prompts.md          the test prompts
└── iteration-1/
    └── <eval-name>/          a descriptive name, not "eval-0"
        ├── eval.md           prompt + assertions for this test
        ├── with_skill/       outputs of the run that used the skill
        ├── without_skill/    outputs of the baseline (or old_skill/)
        └── grading.md        verdict per assertion, with evidence
```

Create folders as you go. Start `iteration-2/` after each change to the skill.

## 2. Test prompts

- Write 2-3 prompts a real user would type: concrete, with file names, column names, paths, a little backstory. Casual phrasing and typos are fine.
- Bad: "Format this data". Good: "my boss sent `Q4 sales final v2.xlsx` - add a column with profit margin as a percentage; revenue is column C, costs column D".
- Skills with checkable outputs (file transforms, extraction, code generation, fixed workflows) need assertions. Subjective outputs (writing style, design) are judged by reading; do not force assertions onto them.

## 3. Running with and without

Launch every run for an iteration in the **same turn**, so they finish together. For each
prompt, two `Agent` calls with `subagent_type: "general-purpose"`:

```
With skill:
Read <workspace>/draft/<name>/SKILL.md and follow it to do this task.
Task: <prompt>
Input files: <paths, or "none">
Save every output to <workspace>/iteration-N/<eval-name>/with_skill/
When done, list the files you wrote and the steps you took.
```

```
Baseline:
Do this task.
Task: <prompt>
Input files: <paths, or "none">
Save every output to <workspace>/iteration-N/<eval-name>/without_skill/
When done, list the files you wrote and the steps you took.
```

- New skill → the baseline has no skill.
- Improving an existing skill → before editing, `cp -r` the old skill to `skill-snapshot/`, and point the baseline at the snapshot (`old_skill/` folder).
- The baseline must not be able to load the draft: keep the draft out of `.claude/skills/` and the bridge library until it is tested.
- No token limit on Agentaus: when results vary between runs, run each configuration 3 times (`run-1/`, `run-2/`, `run-3/`) and compare pass rates as mean and spread.
- Note each run's tool-call count and time if the result reports them; a skill that doubles the steps for the same result is not an improvement.

## 4. Writing assertions

Write them while the runs are in progress, into each `eval.md`.

- Objectively checkable: "`out.csv` exists and has 1,204 rows", "every claim in `report.md` has a `path:line` citation", "no answer in `answers.json` is null".
- Discriminating: an assertion that a clearly wrong output would also pass is worse than none. "The file exists" is weak; "the file has the 12 expected columns" is strong.
- Named so a reader knows what it checks without the transcript.
- Checked by a script when possible (row counts, keys present, schema valid); scripts are faster and reusable across iterations.

## 5. Grading

For each run, for each assertion:

1. Look for evidence in the output files first, then in the run's report. Open non-text outputs with a tool; do not trust the report's description of them.
2. **PASS** only with clear evidence of real completion: right content, not just the right file name.
3. **FAIL** when there is no evidence, the evidence contradicts it, or it is only superficially met.
4. Write the verdict and the evidence (quoted text, a count, a command's output) to `grading.md`.

Also note, briefly:

- claims in the output that you could not verify;
- an assertion that passed but would also have passed for a wrong output;
- an important outcome, good or bad, that no assertion covers.

## 6. Comparing and deciding what to change

Tabulate pass rates per configuration, then look for patterns the totals hide:

- An assertion that passes in both configurations does not measure the skill; replace it.
- One that fails in both may be broken, or beyond the task; check it.
- Passes with the skill, fails without → the skill adds value there; keep that part.
- Fails with the skill, passes without → the skill hurts there; find the step responsible.
- High variance across runs → a vague step or a flaky assertion.

Then improve the skill:

1. **Generalise from the failure.** The skill will meet many prompts; a fix that only works for the test prompt is useless. Prefer a clearer step or a better pattern over a narrow rule.
2. **Cut what does not pull its weight.** If transcripts show the skill making the model do unproductive work, delete that part and re-test.
3. **Explain the why** in half a line where the model skipped a step.
4. **Bundle repeated work.** If most runs wrote the same helper, add it to `scripts/`.

Re-run every prompt, baseline included, into the next `iteration-N/`.

## 7. Blind A/B comparison

For "is the new version actually better?":

1. Put the two outputs for the same prompt in folders `A/` and `B/`, assigning versions to letters at random; record the mapping somewhere the judge cannot see.
2. Launch one `Agent` (`subagent_type: "general-purpose"`) with the task prompt, both folders and this brief:

```
Two outputs for the same task are in A/ and B/. You do not know how either was produced.
Write a rubric of 4-6 criteria from the task itself (correctness, completeness, format, ...).
Score each output 1-5 on each criterion, citing evidence from the files.
Name the winner, or TIE, and give the single biggest reason.
```

3. Unmask. If the winner is the new version, read both transcripts to learn why; if the old one wins, find what the new version lost.

## 8. Trigger-query sets

A description is tested by the requests it should and should not match.

1. Write 16-20 queries, realistic and specific (paths, context, casual wording):
   - 8-10 **should trigger**: different phrasings of the same need, some that never name the skill or file type, some uncommon uses, and cases where this skill competes with another and should win.
   - 8-10 **should not trigger**: near misses that share words or concepts but need something else. "Write a fibonacci function" is useless as a negative for a PDF skill; "extract the table from this Word file" is a good one.
2. Save them as a table (`query | should_trigger`) in `evals/triggers.md`.
3. For each query, read only the listing line and the routing line (the validator prints the latter) and decide whether a model would load the skill.
4. Rewrite the `Use when` sentence until every should-trigger query matches and no near miss does. Keep it under 230 characters.
5. Optional live check once installed: launch one `Agent` per query in the same turn, each told "Before doing anything, name the skill you would load for this request, if any, then stop." Count matches. It measures stated choice, not behaviour, so use it to compare two descriptions rather than as an absolute score.

Note: simple one-step requests ("read this file") rarely trigger any skill, however well the
description matches. Make trigger queries substantive enough that a skill would help.

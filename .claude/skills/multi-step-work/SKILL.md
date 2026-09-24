---
name: multi-step-work
description: Carry a task with many dependent steps through to the end without losing the thread - a pipeline, a migration, a report built from several analyses, anything where step six depends on step two. Use when a request has more than about three parts, says "and then", lists numbered requirements, or asks for several files to be produced together. Covers keeping a task list, working one item at a time, and finishing rather than stopping at the first plausible result.
---

# Getting to the end of a long task

Long tasks do not fail on the hard step. They fail because the plan was never written
down, so by step six the objective has quietly become whatever the last tool result was
about. Measured across agent benchmarks, per-step competence does not compose: an agent
can get most individual steps right and still fail the task.

## Write the list first, then work it

**Call `TodoWrite` before the first action**, on anything with more than about three
parts. One item per deliverable, in dependency order. This is not ceremony — the list is
reflected back to you on later turns, so it is the one part of the plan that cannot fall
out of the conversation.

Then, every turn:

1. **Read the list.** The next unfinished item is the next thing you do.
2. **One item at a time.** Mark exactly one `in_progress`. Attempting several at once is
   the single most common way a long task ends up half-done.
3. **Finish it, verify it, mark it `completed`.** Then update the list.
4. **Do not stop while an item is unfinished.** "I have something to report" is not the
   stopping condition. "Nothing is left on the list" is.

If the work reveals a step you did not anticipate, add it to the list rather than doing
it silently — an item that was never recorded is one nobody can see was skipped.

## Verify with something that measures

A step is done when a check says so, not when the code that should have done it ran
without complaining. Prefer a check that returns a number or a file:

| Claim | The check |
| --- | --- |
| "the chart is written" | `ls -l` it, and open it to confirm its size and that it is not blank |
| "the script works" | run it and read the exit status and output, not just the absence of a traceback |
| "the table is right" | re-derive one figure a second way and compare |
| "the file has the sections" | grep the headings out of it |

The more quantitative the check, the less room there is to conclude success from
something that merely did not fail.

## Leave a clean state

If the work spans sessions, or might, end each one somewhere a successor can pick up:
the script saved rather than held in a heredoc, the intermediate output written to disk,
and a line saying what is done and what is next. A half-finished step with nothing
recorded costs the next session more than it saved this one.

## Rules

- The task list goes in before the first tool call, not after the work is underway.
- One item `in_progress` at a time.
- Do not mark an item complete on the strength of the code you wrote for it. Check it.
- Do not finish the turn with unfinished items unless you say plainly which are left and
  why.
- Adding a discovered step to the list is progress. Doing it without recording it is not.

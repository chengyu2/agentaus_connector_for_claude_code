---
name: plan
description: Break a request into subtasks, judge how hard each is, and decide how many solvers it needs and how their answers are merged.
---

# Plan

You decide the shape of the work. Others will do it. **Do not solve anything.**

## How to split

- One subtask per separate **output**: each question, each value, each bug, each function,
  each section of a document. A value that takes many steps is still **one** subtask.
- Never a subtask that is only a step ("load the file", "read the README").
- Fewer is better when the work is simple. A request with one easy answer has one subtask.
- If the request says there may be problems nobody reported, add one subtask per part of the
  code to audit.
- Give a subtask `Depends on` only when it truly needs another's result.

## How hard, and how many solvers

| Difficulty | Means | Solvers |
| --- | --- | --- |
| easy | one lookup or one obvious step; hard to get wrong | 1 |
| medium | several steps, or one trap to avoid | 2-3 |
| hard | traps, edge cases, ambiguity, or a wrong answer would look right | 3-5 |

More solvers only help when they can disagree usefully. Spend them where a wrong answer is
likely and costly.

## Check

`Check: yes` when a wrong answer would look right (numbers, code, facts quoted from documents).
`Check: no` for easy subtasks with nothing to verify.

## Merge

Choose one strategy from the **merge** skill for each subtask with more than one solver:
`vote`, `best-evidence`, `combine`, `union`, or `pick-change`.

## Write the plan in exactly this Markdown

```markdown
# Plan

## Deliverables
- `path` - what it must contain

## Subtask 1: <short title>
- Difficulty: easy | medium | hard
- Solvers: <1-5>
- Check: yes | no
- Merge: vote | best-evidence | combine | union | pick-change
- Kind: value | text | code
- Depends on: none | <subtask numbers>

### Task
<What the solver must produce, standing alone: the exact question, key, function or
section; the files to use; every rule from the request that applies; the format.>

## Subtask 2: ...

## Assembly
<How the deliverables are built from the subtask results: which result goes where, the
exact file format, keys and order.>
```

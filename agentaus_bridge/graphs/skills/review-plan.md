---
name: review-plan
description: Compare two plans for the same request and write the one plan the work will follow.
---

# Review plans

Two planners planned the same request. Write the plan the work will follow.

## Check each plan against the request

- [ ] Every question, value, bug, function, section and deliverable the request asks for has
      a subtask. List anything missing.
- [ ] No subtask is only a step, and no two subtasks produce the same output.
- [ ] Each `### Task` stands alone: exact question or key, files, rules, format.
- [ ] Difficulty and solver counts are sensible: easy work is not given five solvers, and a
      trap-laden value is not given one.
- [ ] `## Assembly` says exactly how the deliverables are built.

## Then

Take the better plan as the base, add whatever the other caught that it missed, fix every
problem above, and write only the plan, in **exactly** this Markdown:

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

## Assembly
<How the deliverables are built from the subtask results.>
```

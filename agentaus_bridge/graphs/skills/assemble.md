---
name: assemble
description: Build the deliverables from the merged subtask results, applying code changes exactly as told.
---

# Assemble

Write the deliverables the plan names, from the merged results you are given.

## Rules

- Use only the merged results and the request. Do not re-solve anything.
- Follow `## Assembly` in the plan exactly: file names, format, keys, order.
- **Code changes are already in place** - the runtime applied them before you started. Make
  by hand, with small `Edit`s, only a change marked `FAILED`, from that subtask's Answer. Never
  copy a file over a project source file and never rewrite a whole source file.
- **Work through the checklist.** Every item in it - each **bold** key value - goes into a
  deliverable exactly as written. Do not round, reformat or recompute it.
- A value goes in exactly as the merged result gives it. Prose is written from the merged
  sections, keeping every fact and its source.
- After writing, read each deliverable back and check every required part is there. Run code
  you changed once.

## Write back, in Markdown

```markdown
## Written
- `path` - what it now contains

## Problems
- anything missing or that failed, or "none"
```

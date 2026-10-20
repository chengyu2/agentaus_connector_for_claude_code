---
name: assemble
description: Build the deliverables from the merged subtask results, applying code changes by patch.
---

# Assemble

Write the deliverables the plan names, from the merged results you are given.

## Rules

- Use only the merged results and the request. Do not re-solve anything.
- Follow `## Assembly` in the plan exactly: file names, format, keys, order.
- **Code changes only by patch.** Apply each chosen patch with
  `cd PROJECT && patch -p1 --forward -F 3 < PATCH`. If one is rejected, make the same change
  by hand with small edits. Never rewrite a whole source file.
- After writing, read each deliverable back and check every required part is there. Run code
  you changed once.

## Write back, in Markdown

```markdown
## Written
- `path` - what it now contains

## Problems
- anything missing or that failed, or "none"
```

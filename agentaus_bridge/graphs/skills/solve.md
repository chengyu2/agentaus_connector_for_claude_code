---
name: solve
description: Produce one subtask's result with tools, showing the evidence.
---

# Solve one subtask

## Rules

- **Do the work with tools.** Run code, open files. Never estimate, never describe what you
  would do, never say a file is missing without listing the folder.
- **The project folder is read-only.** For code, copy it first and change only the copy.
- A `0` is a value; a blank is not `0`. An id with spaces and without is the same id.
- Use only facts you can show. Where the files do not hold the answer, say so plainly.

## For a code subtask

1. Copy: `rm -rf COPY && mkdir -p COPY && cp -R PROJECT COPY/repo` (paths are given to you).
2. Show the problem in the copy, fix it with small edits (never rewrite a whole file), and
   run it again to show it works.
3. Save the change: `cd COPY && diff -ruN -x __pycache__ -x .git PROJECT repo | sed -e "s#^--- PROJECT#--- a#" -e "s#^+++ repo#+++ b#" > fix.patch`
   (the exact command is given to you).

## Write back, in Markdown

```markdown
## Answer
<the result itself: the value, the finished text, or what was wrong and how it is fixed>

## Evidence
<the commands you ran and what they printed, or the passages you used>

## Change
<`path/to/fix.patch`, or "none">
```

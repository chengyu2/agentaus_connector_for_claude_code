---
name: check
description: Check one result or one deliverable against what was asked, with code, and give a verdict.
---

# Check

You check work someone else did. **Run code to check it** - do not judge by eye.
**You never edit, write or patch any file**; an edit you make is undone and fails your check.

## Steps

1. Read what was asked, exactly.
2. Recompute or re-test it your own way: a different method from the one in the evidence
   where you can. Test the traps: zero values, blanks, duplicates, edge cases, every
   required key or question present.
3. **The spec wins.** For code, the docstring and README are the spec: a result that breaks a
   docstring example is a FAIL, whatever the task's wording says. For text, find each fact in
   the source yourself (`grep -n`); if the result says something is absent, grep for it - if
   you find it, FAIL and quote the line. If the brief has Stated outputs, run the program and
   compare each row.
4. Machine-readable output (JSON, CSV): parse it with the real parser, never by eye.
5. Decide.

## Write back, in Markdown

```markdown
**Verdict:** PASS | FAIL

## Problem
<exactly what is wrong, or "none">

## Checklist
<only if you were given a checklist: every item, `[x]` if it is right, `[ ]` and what you found if not>

## Evidence
<the commands you ran and what they printed>
```

`FAIL` only when your evidence **shows** a problem. If you could not check, say `PASS` and
explain under Problem.

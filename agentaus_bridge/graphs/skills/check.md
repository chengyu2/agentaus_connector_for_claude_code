---
name: check
description: Check one result or one deliverable against what was asked, with code, and give a verdict.
---

# Check

You check work someone else did. **Run code to check it** - do not judge by eye - and
change nothing.

## Steps

1. Read what was asked, exactly.
2. Recompute or re-test it your own way: a different method from the one in the evidence
   where you can. Test the traps: zero values, blanks, duplicates, edge cases, every
   required key or question present.
3. Decide.

## Write back, in Markdown

```markdown
**Verdict:** PASS | FAIL

## Problem
<exactly what is wrong, or "none">

## Evidence
<the commands you ran and what they printed>
```

`FAIL` only when your evidence **shows** a problem. If you could not check, say `PASS` and
explain under Problem.

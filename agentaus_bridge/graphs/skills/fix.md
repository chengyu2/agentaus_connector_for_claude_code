---
name: fix
description: Fix one problem a checker found in the deliverables, without breaking anything else.
---

# Fix one problem

1. **Show the problem with your own code.** If you cannot reproduce it, change nothing and
   say so.
2. Change only what the problem needs, with small edits. Keep every other part as it is.
3. A merged result was reached by several solvers: change it only if your code proves it
   wrong, and say so.
4. Run the check again to show the problem is gone. If you were given repros, run every one
   before and after: your change is measured by them, by the docstring examples, by the
   imports and by the solvers' tests, and it is undone if fewer repros do not pass or anything
   that passed before now fails.
5. **Never change a function so that one of its docstring examples breaks**, and never edit a
   docstring: a repro that disagrees with an example is the repro's mistake - say so.

## Write back, in Markdown

```markdown
## Fixed
<what you changed, or "nothing - could not reproduce">

## Evidence
<what you ran before and after>
```

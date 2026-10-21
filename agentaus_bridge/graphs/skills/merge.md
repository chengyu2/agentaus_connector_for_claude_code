---
name: merge
description: Merge several solvers' Markdown results for one subtask into one result, by the strategy the plan chose.
---

# Merge results

Several solvers worked the same subtask independently. Write the one result the work keeps.

## Strategies

| Strategy | Use for | How |
| --- | --- | --- |
| `vote` | a value | Group the answers that are the same value (ignore formatting: `1,000` = `1000`). The largest group wins **if** its evidence holds up. A tie, or a winner whose evidence is weak, goes to `best-evidence`. |
| `best-evidence` | a value or fact where solvers disagree | Keep the answer whose evidence actually shows it - a command that ran and printed it, a quoted passage. Prefer evidence that handles the traps (zero values, blanks, duplicates, edge cases). |
| `combine` | text | Build one text from the best parts of each: every correct fact once, each with its source; drop anything only one solver claims without evidence. |
| `union` | lists | Every distinct item any solver found with evidence; drop duplicates. |
| `pick-change` | code | **Run it.** Pool every solver's tests into one set. Run each candidate change against the whole pool (each in its own copy, as told). Keep the candidate that passes the most; on a tie, the smallest change. A test the reference examples in the docstring contradict is wrong - drop it. |

## Rules

- **Keep text verbatim.** Copy the sentences you keep from the result you keep: the same
  words, figures and plain keyboard characters (`-`, not a special hyphen), and every citation
  exactly as written. Never reword a sentence you are keeping.
- For code, the docstring and its examples are the spec: a candidate that fails an example,
  or that edited the docstring, loses.
- Never average numbers or blend two different values.
- Never add a fact no solver showed.
- If every answer is wrong or empty, say so plainly rather than choosing one.

## Write back, in Markdown

```markdown
## Answer
<the merged result>

## Key values
- **<name>:** <value>
- **<name>:** <value>

## Why
<which answers agreed, which evidence or test results decided it>

## Change
<the chosen change path, exactly as a solver gave it, or "none">
```

Under `## Key values`, list every value the merged answer settles, each exactly as the
winning result gives it - the next agents copy these into the deliverable.

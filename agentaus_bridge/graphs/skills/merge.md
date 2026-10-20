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
| `pick-change` | code | Choose the change that fixes the subtask with the smallest diff and nothing else changed. Name its patch file exactly. |

## Rules

- Never average numbers or blend two different values.
- Never add a fact no solver showed.
- If every answer is wrong or empty, say so plainly rather than choosing one.

## Write back, in Markdown

```markdown
## Answer
<the merged result>

## Why
<one or two lines: which answers agreed, which evidence decided it>

## Change
<the chosen patch file path, or "none">
```

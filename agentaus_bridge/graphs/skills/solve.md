---
name: solve
description: Produce one subtask's result with tools, showing the evidence.
---

# Solve one subtask

## Rules

- **Do the work with tools.** Run code, open files. Never estimate, never describe what you
  would do, never say a file is missing without listing the folder.
- **Never touch the project folder.** Work only in the copy you are given; anything written
  in the project is undone before assembly, and your result is lost with it.
- **Do every step yourself, from the original files.** Nobody prepared anything for you.
- A `0` is a value; a blank is not `0`. An id with spaces and without is the same id. Load
  tables with pandas `dtype=str, keep_default_na=False`, then convert the columns you need.

## A value

1. Compute it end to end from the original files.
2. **Compute it a second way** in the same run (another library, another grouping, a check
   total that must match) and make the two agree before you answer.

## Text from documents

Read the document text itself (a plain-text copy may be given). Quote facts exactly and cite
the file after each, in the form the request uses (e.g. `[proposal]`).
**Before you write that the documents lack something, `grep -in` its key words in every
source file.** If you find it, answer from it - even if your subtask says otherwise - and say
the subtask was wrong. Write amounts with comma thousands separators (`$80,000`) unless the
request says otherwise. Where the documents do not hold the answer, say so - never invent.

## Code

1. Copy the project as told, and read the code you are changing.
2. **Write tests first**: every example in the docstring, copied verbatim - **an example in the
   docstring is the specification: if your reading of the prose disagrees with an example,
   your reading is wrong** - plus the edge cases it implies -
   empty input, zero, negatives, duplicates, single items, ties, rounding, upper and lower
   case. Run them and watch the old code fail.
3. Change the code with small edits - never rewrite a whole file - until every test passes.
   **Never edit a docstring, README or comment to match your change, and never change a
   function's name, arguments or defaults.**
4. Save the change exactly as told (one function: save that function; otherwise leave it in
   your copy's `repo` - never write a patch by hand), and save your tests to the tests file
   you are given.

**When the obvious reading fails a docstring example, the reading is wrong - not the example.**
Write down at least three other readings of the words (how a sign, a tie, a boundary or a
rounding rule could be meant), code each as a three-line function, and run every example
against each. Keep the one reading that passes **all** of them. Never branch on an example's
literal input, and never add a rule that exists only to hit one example's output.

**Every implementation's tests include the traps:** "the word X" versus "starts with the
letters X" (`"Is"` is not the word `"I"`), how a negative number's sign and digits count,
ties, and the boundary itself.

**A task sentence that says what the code does now** ("counts ranks `< k`", "computes the
median") describes the code under audit, not the spec. The function's name is part of the
spec: `mean_*` is a mean; "top k" includes the k-th. Never add or rewrite a docstring.

**The planner's notes are a paraphrase and may be wrong.** Where they disagree with the spec
(the docstring, the question, the README), follow the spec and say so under `## Evidence`.

**Auditing a function for bugs nobody reported?** Read it line by line against its name, its
docstring and the README, and check each of these:

- [ ] every comparison boundary: `<` versus `<=` ("top ten" includes the tenth), `>` versus `>=`
- [ ] off-by-one in ranges, slices and indexes; 0- versus 1-based ranks
- [ ] sort direction and tie-breaks (which comes first when two are equal)
- [ ] `0`, empty and `None` treated as false where they are real values
- [ ] mean versus median, sum versus count, the wrong field or key
- [ ] spelling variants and string normalisation (case, spaces, accents, US/UK spellings)
- [ ] a mapping or table: write each group's expected members and size **from a source other
      than this code** - the README, the documents in the project, the public model the request
      names - with the source beside each. Then print what the code has and compare. An
      expected column copied from the code means the audit is not done.
- [ ] every prose example in a docstring or README ("X reads as Y"): run the code on exactly
      that input and check it gives exactly that result
- [ ] every numeric threshold or constant, tried on the real cases the README names
- [ ] "the word X" versus "starts with the letters X"; how a negative number's sign and digits count
- [ ] a sibling that measures the same thing: totals that must agree (e.g. a count of "in the
      top k" over everything must equal the slot total another function reports) - compute both
- [ ] the README's stated outputs (counts, totals): run the program and compare

If you find none, say "no bug" and show the tests that pass.

## Write back, in Markdown

```markdown
## Answer
<the result itself: the value, the finished text, or what was wrong and how it is fixed>

## Key values
- **<name>:** <value>
- **<name>:** <value>

## Evidence
<the commands you ran and what they printed: both methods for a value, the test run for code>

## Change
<the saved function or patch path, or "none">

## Tests
<the tests file path, or "none">
```

**`## Key values` is what the next agents carry forward** - anything not listed there can be
lost. List every number, amount, date, name, yes/no and file path your answer settles, one
per line, exactly as it must appear in the deliverable (`- **Fee inc GST:** $88,000`,
`- **is_bored fixed:** yes`). Keep the headings exactly as above.

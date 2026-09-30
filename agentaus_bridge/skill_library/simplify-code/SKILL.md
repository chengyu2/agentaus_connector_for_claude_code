---
name: simplify-code
description: Simplify recently changed code for clarity, consistency and maintainability without changing what it does - flatten nesting, remove redundancy, clarify names, replace nested ternaries, drop comments that restate the code - following the project's own conventions, and prove behaviour is unchanged by running the tests before and after. Use when asked to simplify, clean up, tidy, refactor for readability, reduce nesting or make code clearer, or to polish code that was just written or changed, or to apply simplify suggestions from a review. Not for fixing bugs, adding features or tuning performance.
license: Apache-2.0
metadata:
  source: "anthropics/claude-plugins-official plugins/code-simplifier/agents/code-simplifier.md"
  adapted-for: agentaus
---

# Simplify code without changing what it does

## When to use

- "Simplify this", "clean up my changes", "tidy this function", "make this readable",
  "this is too nested", "refactor for clarity", "polish what you just wrote".
- After `thorough-code-review` returned simplify suggestions the user wants applied.
- Not when behaviour should change (bug fix, feature, speed-up). Do that first, separately.

## The one rule

Change **how** the code does it, never **what** it does. After your edits the code must
have the same return values, the same side effects in the same order, the same errors
raised, the same public names and signatures, and the same output and logging.

## Steps

1. Make a task list with `TodoWrite`: one item per step.
2. Fix the scope. Use the first line that applies:
   - The user named files, functions or lines: exactly those.
   - You edited files earlier in this conversation: those edits.
   - Otherwise: `git diff --name-only`, `git diff --staged --name-only` and
     `git ls-files --others --exclude-standard`. Work only on the changed hunks
     (`git diff -U0 -- <file>`), not whole files.
   - All empty: ask what to simplify, and stop.
3. Learn the conventions. `Read` any `CLAUDE.md`, `AGENTS.md` or `CONTRIBUTING.md`, the
   linter and formatter config (`.editorconfig`, `pyproject.toml`, `.eslintrc*`,
   `.prettierrc*`, `rustfmt.toml`), and one unchanged file next to each changed file.
   Write down 3 to 8 conventions: naming, function style, error handling, import order.
   No guideline files: the surrounding code is the standard.
4. Find the test command, run it with `Bash`, and record the baseline: number passed,
   number failed, names of failing tests.

   | You see | Run |
   | --- | --- |
   | `pytest.ini`, `pyproject.toml` with pytest, or `test_*.py` files | `python -m pytest -q` |
   | `package.json` with a `test` script | `npm test` (or `pnpm test` / `yarn test`, matching the lockfile) |
   | `go.mod` | `go test ./...` |
   | `Cargo.toml` | `cargo test` |
   | `Makefile` with a `test` target | `make test` |

   If a linter or type checker is configured (`ruff check`, `mypy`, `eslint`,
   `tsc --noEmit`), run it too and record its baseline.
5. List candidates. For each in-scope hunk, go through the checklist below. Add each
   candidate to the task list as `path:line - what - why simpler`.
6. More than 5 files in scope: first launch one `Agent` per file, all in one turn, with
   `subagent_type: "general-purpose"`. Paste the one rule, the checklist, the "Do not"
   list, your conventions and the file's hunks. Ask for candidates only, no edits, in
   the format `path:line | current code | simpler code | why behaviour is unchanged`.
   Add the ones you agree with to the task list.
7. Apply one candidate at a time with `Edit`. After each edit, record the before and
   after snippets.
8. After finishing each file, run the tests again and compare with the baseline:
   - Same results: keep the edits.
   - A test that passed now fails: `Edit` the file back using your before snippets,
     drop that candidate, and note it under "Left alone".
9. Independent check. Launch one `Agent` (`subagent_type: "general-purpose"`) with
   every before/after pair, the file paths and the one rule. Ask it to list any pair
   that could change behaviour: return values, side effects or their order, exceptions,
   types, truthiness, short-circuit evaluation, laziness, public API. For each pair it
   flags, `Read` the code. If you cannot show it is safe, revert it.
10. Final run: the tests and any linter or formatter. Then read `git diff` for the
    in-scope files once, end to end.
11. Report with the template below.

## Checklist: what to simplify

- **Deep nesting** → guard clauses and early returns.
- **Nested ternaries** → an `if` / `else` chain, or `switch` / `match`.
- **Redundancy** → remove duplicate branches, repeated expressions, needless wrappers,
  and variables used once that add no meaning.
- **Dead code** → remove it only after `Grep` shows no references anywhere, including
  tests, string-based lookups and exports.
- **Unclear names** → names that say what the value holds. Local names only, unless the
  user asked for public renames.
- **Scattered logic** → bring related steps together.
- **Comments that restate the next line** → remove. Keep comments that explain why.
- **Boolean clutter** → `if x == True:` becomes `if x:`; `return True if c else False`
  becomes `return c`, but only when `c` is already a boolean.
- **Inconsistency** → match the conventions you wrote down in step 3.

## Do not

- Do not change public function names, signatures, exported symbols or file names.
- Do not trade clarity for fewer lines: no dense one-liners, no clever tricks.
- Do not merge unrelated concerns into one function, or remove an abstraction that
  organises the code.
- Do not touch code outside the scope.
- Do not fix things that look like bugs. List them under "Noticed, not changed".
- Do not reformat whole files. If the project has a formatter, run it instead.

## If this → do that

- Tests already fail at baseline → do not fix them. The same tests, and only those, must
  fail at the end.
- No test suite → tell the user before starting. Make only changes whose equivalence is
  visible in the code itself: guard clauses, un-nesting ternaries, renaming locals,
  removing unreferenced code. Write "behaviour not verified by tests" in the report.
- `return True if x else False` where `x` may not be a boolean → leave it, or write
  `return bool(x)`.
- `a and b or c` used as a ternary → not the same as `b if a else c` when `b` can be
  falsy. Leave it unless you can show `b` is always truthy.
- Loop to comprehension or generator → leave it if the loop has side effects, breaks
  early, or relies on eager evaluation.
- A simplification is only worth it with a behaviour change → do not make it. List it
  under "Left alone".
- Code on a hot path where the simpler form allocates or computes more → leave it, and
  say why.

## Done when

- Every in-scope hunk was checked against the checklist.
- The tests give the same results as the baseline, or you reported that there are none.
- The independent check returned, and every pair it flagged is resolved.
- The report lists every change with `path:line`.

## Report template

~~~markdown
## Simplified <n> places in <m> files

Tests: `<command>` - before: <p> passed, <f> failed; after: <p> passed, <f> failed.
Lint/types: `<command>` - before: <result>; after: <result>.

### Changes
1. `path:line` - <what changed> - <why it is simpler>

### Left alone
- `path:line` - <candidate> - <why it was not changed>

### Noticed, not changed
- `path:line` - <possible bug or behaviour question for the user>
~~~

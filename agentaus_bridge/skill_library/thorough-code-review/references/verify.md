# Verifying a review finding

Use this to decide whether a finding is real before it goes into the report. If you
launch a verifier subagent, paste this whole file into its prompt, together with the
finding, the repository root, the changed-file list and the guideline file paths.

## Steps

1. `Read` the cited file at the cited lines, with at least 20 lines of context on each
   side. For a long file, use `Read` with `offset` and `limit`, or `agentaus_zoom`.
2. Confirm that the quoted evidence is actually there, word for word.
3. Confirm that the problem really follows from that code. Look for what would prevent
   it: a guard earlier in the function, a check in the caller, a type that rules the
   input out, a test that pins the behaviour.
4. If the finding depends on code outside those lines (a caller, a config value,
   another module), read that code too. Use `Grep` to find it.
5. If the finding cites a guideline rule, open the guideline file and find the rule.
   It must say this specifically. If you cannot find it word for word, score it 0.
6. Check the finding against the false-positive list below.
7. Give it a score from the scale below.

## Confidence scale

- **0 - Not confident at all.** A false positive that does not survive light scrutiny,
  or an issue that existed before the change.
- **25 - Somewhat confident.** Might be real, might be a false positive; you could not
  verify it. If stylistic, it is not explicitly called out in a guideline file.
- **50 - Moderately confident.** You verified it is real, but it is a nitpick or will
  rarely happen in practice. Not very important relative to the rest of the change.
- **75 - Highly confident.** You double-checked it and it is very likely real and will
  be hit in practice. The current code is insufficient. It directly affects
  functionality, or a guideline file directly mentions it.
- **100 - Certain.** You double-checked it and it is definitely real and will happen
  often. The evidence directly confirms it.

Keep findings from the bugs, guidelines and history lenses only if they score **80 or
more**. For the errors, tests, types, comments and simplify lenses, keep a finding if
steps 2 and 3 confirm it; those lenses use their own severity scales.

## False positives: drop these

- Issues that existed before the change.
- Something that looks like a bug but is not.
- Pedantic nitpicks a senior engineer would not raise.
- Issues a linter, type checker or compiler would catch: missing or wrong imports, type
  errors, broken tests, formatting, style such as blank lines. Assume those checks run
  separately.
- General quality issues (test coverage, general security, documentation) from the bugs,
  guidelines or history lenses, unless a guideline file requires them. The tests,
  errors and comments lenses exist for those.
- Issues a guideline file calls out but the code explicitly silences, for example with
  a lint-ignore comment.
- Behaviour changes that are clearly intentional and part of the change's purpose.
- Real issues on lines the change did not modify. Exception: new code that sends
  untrusted data into an existing dangerous call (shell, SQL, `eval`, HTML output).
  The new data path is the bug.

When the review scope is "these files in full" (no diff), every line is in scope and
the "did not modify" items do not apply.

## Verifier report format

One line per finding:

```text
path:line | score 0-100 | keep or drop | one-line reason | quoted evidence you read
```

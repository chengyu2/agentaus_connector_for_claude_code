---
name: thorough-code-review
description: Review a code change with several independent reviewers in parallel (bugs, project guidelines, git history, error handling, tests, type design, comments, simplification), then verify every finding against the code and report the survivors ranked by severity with file and line. Use when asked to review code, a diff, uncommitted changes, a branch or a pull request - "review my changes", "review PR 123", "check this before I commit", "is this ready to merge", "any bugs in this file". Works from git diff, a GitHub PR through gh, or named files.
license: Apache-2.0
metadata:
  source: "anthropics/claude-plugins-official plugins/code-review/commands/code-review.md; plugins/pr-review-toolkit/commands/review-pr.md; plugins/pr-review-toolkit/agents/{code-reviewer,silent-failure-hunter,pr-test-analyzer,type-design-analyzer,comment-analyzer,code-simplifier}.md"
  adapted-for: agentaus
---

# Thorough code review

Several reviewers, one lens each, read the change at the same time. Then you check every
finding against the code yourself, drop what does not hold up, and report the rest.

## When to use

- "Review my changes", "review this diff", "review PR 123", "check before I commit",
  "is this ready to merge", "find bugs in `src/auth.ts`".
- Before you call your own non-trivial change finished.
- "Just check the error handling" or "just the tests": still this skill, but run only
  those lenses (step 5).

## The lenses

Each lens has a full reviewer prompt in `references/`.

| Lens | Reference | Run it when |
| --- | --- | --- |
| bugs | `references/bugs.md` | always |
| guidelines | `references/guidelines.md` | step 4 found a guideline file |
| history | `references/history.md` | the scope is a diff or PR in a git repo |
| errors | `references/errors.md` | change touches try/catch/except, error returns, fallbacks, defaults on failure, retries, `?.`, `??` |
| tests | `references/tests.md` | change adds or edits logic in source files, or edits tests |
| types | `references/types.md` | change adds or edits a class, interface, type alias, struct, enum, dataclass, schema or model |
| comments | `references/comments.md` | change adds or edits comments, docstrings or API docs |
| simplify | `references/simplify.md` | change adds 20 or more lines of non-test source |

## Steps

1. Make a task list with `TodoWrite`: one item per step.
2. Fix the scope. Use the first line that applies:
   - PR number or URL: `gh pr view <n> --json number,title,state,isDraft,baseRefName,headRefOid,url`, then `gh pr diff <n>`.
   - Files or folders named: review those files in full. There is no diff.
   - Branch or commit named: `git diff <base>...<branch>` or `git show <sha>`.
   - Nothing named: `git diff` plus `git diff --staged`. Both empty: `git diff origin/HEAD...HEAD`. Still empty: ask what to review, and stop.
3. Record the repository root (`git rev-parse --show-toplevel`), the changed-file list
   (same command with `--name-only`), and the diff text.
4. Find guideline files: `Glob` for `**/CLAUDE.md`, `**/AGENTS.md`, `**/CONTRIBUTING.md`.
   Keep the root ones and those in a directory that holds a changed file, or a parent of one.
5. Choose lenses. Go down the table and write `run` or `skip: <reason>` for each.
   If the user named aspects, run only those: "bugs" = bugs; "style", "conventions",
   "CLAUDE.md" = guidelines; "regression", "blame" = history; "error handling",
   "exceptions" = errors; "tests", "coverage" = tests; "types", "data model" = types;
   "comments", "docs" = comments; "readability", "cleanup" = simplify.
6. `Read` the reference file of every lens you will run.
7. Launch all chosen lenses **in one turn**: one `Agent` call per lens, each with
   `subagent_type: "general-purpose"`, using the template below. Paste the full text
   of the reference file. The subagent cannot see this skill.
8. Wait for every result. If a lens errored or returned nothing, relaunch it once.
9. Merge into one list. Two findings in the same file within 3 lines about the same
   problem become one finding that names both lenses.
10. Verify each finding yourself:
    1. `Read` the cited lines with 20 lines of context each side.
    2. Confirm the quoted evidence is there and the problem follows from it.
    3. Score it and apply the false-positive list in `references/verify.md`.
    4. If it depends on code you have not seen, give it to a verifier `Agent` with
       `references/verify.md` pasted in. Launch all verifiers in one turn.
11. Rank the kept findings with the severity table.
12. Write the report with the template.

### Subagent prompt template

~~~text
You are one reviewer in a multi-lens code review. Your lens: <lens>.
Do not edit any files. Read any file you need.

Repository root: <absolute path>
Scope: <"uncommitted changes" | "PR 123, base main" | "these files in full">
Changed files:
- <path>
Guideline files: <paths, or "none">

<full text of references/<lens>.md>

The diff:
```diff
<diff text, or: "No diff. Review the changed files above in full.">
```

Answer in the report format from your lens instructions. Every finding needs
path:line and the exact code quoted.
~~~

### Severity table

| Lens | Critical | Important | Suggestion |
| --- | --- | --- | --- |
| bugs, guidelines, history | confidence 90-100 | 80-89 | drop below 80 |
| errors | CRITICAL | HIGH | MEDIUM |
| tests | rating 9-10 | 7-8 | 5-6 |
| types | "invalid state reachable: yes" | any axis rated 4/10 or lower | other improvements |
| comments | - | critical issues | improvements, removals |
| simplify | - | - | every suggestion |

### Report template

~~~markdown
# Code review: <scope>

Reviewed <N> files. Lenses run: <list>. Skipped: <lens (reason)>.

## Critical (<n>)
1. `path:line` - <what goes wrong, and when> [<lens>]
   Fix: <concrete change>

## Important (<n>)
## Suggestions (<n>)

## Strengths
- <what the change does well, with path>

## Dropped on verification (<n>)
- `path:line` - <claim> - <why it did not hold>

## Recommended action
1. Fix critical items. 2. Fix important items. 3. Consider suggestions. 4. Re-run this review.
~~~

If nothing survives: "No issues found. Checked for: <lenses run>."

## If this → do that

- PR is closed or merged → say so in one line and ask whether to review anyway.
- PR is a draft → review it, and note it is a draft.
- `gh` missing or not logged in → `git fetch origin pull/<n>/head`, then `git diff origin/<base>...FETCH_HEAD`.
- `origin/HEAD` does not exist → use `main` or `master`, whichever `git branch -a` lists.
- Diff longer than about 1500 lines → split changed files into groups of about 10 and launch each lens once per group.
- Only lockfiles, generated files or version bumps changed → run the bugs lens only, and say why.
- Finding is on a line the change did not touch → drop it (unless reviewing named files in full).
- New code sends untrusted data into an existing shell, SQL, `eval` or HTML call → keep it. The new data path is the bug.
- Finding is something a compiler, type checker or linter would catch → drop it.
- Guideline finding whose rule you cannot find word for word → drop it.
- User wants a security review, or the change touches auth, request handlers, shell commands, user file paths, deserialization or HTML output → after this review, `Skill` with `skill: "secure-coding"` on the same files.
- User wants the simplify suggestions applied → `Skill` with `skill: "simplify-code"`.
- User asks to post the review on the PR → follow `references/post-to-pr.md`.

## Done when

- Every chosen lens returned, or was relaunched once and is reported as failed.
- You read every kept finding at its cited line yourself.
- Every report item has `path:line`, a lens and a fix.
- Every skipped lens is listed with its reason.

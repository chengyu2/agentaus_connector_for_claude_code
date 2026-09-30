---
name: feature-development
description: Build a new feature in an existing codebase in seven gated phases - discovery, parallel codebase exploration by subagents, clarifying questions, competing architecture designs, implementation after approval, parallel code review, and a summary. Use when asked to add, build, implement or develop a feature, endpoint, page, command, integration or module in an existing codebase, or handed a spec, ticket, issue or PRD to implement that will touch several files. Not for a typo, a one-line fix or a single edit whose location is already known.
license: Apache-2.0
metadata:
  source: claude-plugins-official/plugins/feature-dev
  adapted-for: agentaus
---

# Feature development

Understand the code, ask about what is unclear, compare designs, get approval, build, have it reviewed, report. Seven phases, in order. Do not skip one.

Paths such as `references/code-explorer.md` are relative to this skill's base directory (the `Base directory for this skill:` line above). `Read` them by absolute path.

## When to use

- A new feature, endpoint, page, command, integration or module in an existing repository.
- A spec, ticket, issue or PRD whose implementation touches more than one or two files.
- **Not** a typo, a one-line fix, or one edit whose location you know: make that edit directly.

## Interactive or non-interactive

Decide once, at the start, and write the answer down.

- **Interactive** (default): a person is answering in this conversation. At each **GATE**, send your questions, end your turn, and wait. When the reply comes, continue at the next phase. Do not load this skill again.
- **Non-interactive**: the request says not to ask, to proceed on your own, or approves the plan in advance; or you are yourself a subagent. At each **GATE**, write an `Assumptions` block (each question and the answer you chose) and continue.

## Steps

### Phase 1 - Discovery

1. Call `TodoWrite` with one item per phase: Discovery, Exploration, Questions, Architecture, Implementation, Review, Summary.
2. Write a feature statement of 2-4 sentences: the problem, what the feature does, constraints, and how you will know it works.
3. If you cannot write the "how you will know it works" sentence → **GATE**: ask what problem it solves, what it should do, and what constraints apply.
4. Done when the statement is written and every guess in it is listed as an assumption.

### Phase 2 - Codebase exploration

1. Pick 2-3 different focuses:
   - features similar to this one, traced end to end;
   - architecture and abstractions of the area the feature lives in;
   - the current implementation of the code the feature changes;
   - UI patterns, test approach and extension points.
2. In **one turn**, make one `Agent` call per focus with `subagent_type: "Explore"`. Build each prompt from "Subagent prompt" below, with the full text of `references/code-explorer.md` pasted in.
3. When they return, merge their "Essential files" lists and `Read` every file on the merged list yourself, in parallel.
4. Write a findings summary: entry points (`path:line`), the pattern to copy, where the feature plugs in, how it is tested, risks.
5. Done when you have read every essential file and the summary names the entry point, the pattern and the test approach.

### Phase 3 - Clarifying questions (never skip)

1. Check the statement and findings against: edge cases, error handling, integration points, scope boundaries, UI or API design preferences, backward compatibility, performance, data migration, permissions and security, tests expected.
2. Write each open point as a numbered question with your recommended answer beside it.
3. **GATE**: send the numbered list and wait for answers.
4. If the answer is "whatever you think is best" → reply with your recommendations and ask for an explicit yes.
5. Done when every question has an answer or a written assumption.

### Phase 4 - Architecture

1. In **one turn**, make 2-3 `Agent` calls with `subagent_type: "Plan"`, one per trade-off:
   - **A. Minimal change**: smallest diff, most reuse;
   - **B. Clean architecture**: maintainability, clear abstractions;
   - **C. Pragmatic balance**: speed and quality. For a small feature, A and C are enough.
2. Each prompt carries the feature statement, the answers and assumptions, your findings summary, the essential files, the trade-off, and the full text of `references/code-architect.md`.
3. Compare the blueprints in a table: files touched, new files, new abstractions, risk, test effort, fit with conventions.
4. Choose one. Give the reason in terms of this task: size, urgency, complexity, how the codebase already does it.
5. `Read` each file the chosen blueprint modifies and confirm the functions and lines it names exist. Correct it where they do not.
6. **GATE**: present each approach in 2-3 lines, the table and your recommendation, and ask which to implement. That answer is Phase 5's approval.
7. Done when one blueprint is chosen and approved (or assumed) and its file list matches the repository.

### Phase 5 - Implementation

1. Do not start without Phase 4's approval or its written assumption.
2. Use `TodoWrite` to replace the Implementation item with the blueprint's build steps, one item each.
3. `Read` a file before you `Edit` it. Copy the codebase's naming, error handling, imports and test style.
4. One build step at a time. After each, run the narrowest check that exists: its tests, a type check, a lint, or an import.
5. Add the tests the blueprint names. Find the test command in `README`, `package.json`, `pyproject.toml`, `Makefile` or CI config, and run it.
6. Change nothing outside the blueprint. Do not reformat code you did not need to touch.
7. If the feature is mainly a new UI whose look matters → also call `Skill` with `skill: "frontend-design"`.
8. Done when every build step is complete and the tests pass, or each remaining failure is recorded with why it is pre-existing.

### Phase 6 - Quality review

1. Collect the change set: `git status --porcelain` and `git diff`. Untracked new files are not in `git diff`; list them separately.
2. In **one turn**, make 3 `Agent` calls with `subagent_type: "general-purpose"`, one per focus:
   - simplicity, duplication, readability;
   - bugs and functional correctness (logic, null handling, races, security);
   - project conventions and use of existing abstractions.
3. Each prompt carries the feature statement, the changed and new files, the project guideline files, the focus, and the full text of `references/code-reviewer.md`.
4. Merge duplicate findings. `Read` the cited line of each and drop any that is not real. Sort into Critical and Important.
5. **GATE**: present the issues; ask: fix now, fix later, or proceed as is. Non-interactive → fix every confirmed issue inside the feature's scope and list the rest.
6. After fixing, re-run the tests. If the fixes changed more than a few lines, run one more bug-focused reviewer on the new diff.
7. Done when no confirmed Critical issue is unaddressed (unless the user chose to proceed) and the tests pass.

### Phase 7 - Summary

1. Mark every todo complete with `TodoWrite`.
2. Take the file list from `git status --porcelain`, not from memory.
3. Report:

```text
## What was built
## Key decisions (approach chosen, and why)
## Assumptions made
## Files changed
## Tests run and results
## Open issues and suggested next steps
```

## Subagent prompt

A subagent starts with an empty context: everything it needs goes in its prompt.

```text
You are the <ROLE> for a feature being added to the repository at <ABSOLUTE REPO PATH>.
Work read-only: do not edit, write, stage or commit any file.

## Feature
<feature statement from Phase 1>

## Your focus
<one focus, or one trade-off>

## Context
<Phase 4: answers, assumptions, findings summary, essential files.
 Phase 6: changed files, new files, guideline files.>

## Instructions
<full text of references/<agent>.md, pasted here>
```

If a reference file cannot be read, write its gist yourself: explorer = trace entry point to storage, cite `path:line`, list 5-10 essential files; architect = one decisive blueprint with files, components, data flow, build steps; reviewer = only issues scored 80+ of 100, each with `path:line` and a fix.

## If this → do that

- If `Agent` is not offered → do each subagent's work yourself, one focus at a time, using its reference file as the checklist.
- If a subagent cites a file or line that does not exist → discard that claim and check the area yourself.
- If two blueprints disagree about where the change belongs → `Read` the disputed files, decide, and write down why.
- If tests fail after implementation → fix them before Phase 6. Reviewers should see a passing build.
- If a reviewer flags code the feature did not touch → list it under open issues; do not fix it.
- If the user changes requirements mid-way → return to Phase 3 for the changed part only.

## Done when

- An approach was approved (or assumed in writing), every build step is done, tests pass, review issues are handled, and the summary's file list matches `git status`.

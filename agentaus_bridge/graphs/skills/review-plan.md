---
name: review-plan
description: Compare two plans for the same request and write the one plan the work will follow.
---

# Review plans

Two planners planned the same request. Write the plan the work will follow.

## Fix these in whichever plan you start from

- [ ] **Delete every step subtask** - one that loads, reads, normalises, gathers or prepares
      data, or that "computes per company" for others. Fold what it said into the Task of
      each output that needs it. Each output's solvers work from the original files.
- [ ] **Delete every subtask that assembles, writes or cross-checks** - the runtime does
      those. Move any format detail into `## Assembly`.
- [ ] **One subtask per `## Stated outputs` row whose two columns differ** (see the plan skill).
- [ ] **One subtask per output** the request asks for: every question, key, value, function,
      bug and section. Split "implement all ten functions" into ten, and "fix `tools/bank.py`"
      into one subtask per reported symptom plus, when the request says there may be more,
      one `Audit \`function\`` subtask per function in that file. Add anything missing.
- [ ] **Only what was asked.** Keep a subtask only if it is a reported symptom, a feature the
      request asks to add, an output it names, or an `Audit`. Drop "defensive handling",
      "robustness", "lower a threshold", "log omitted items" and the like.
- [ ] **Deliverables are only what the request names.** Remove any file it did not ask for.
- [ ] **Solvers:** easy 1 (copied straight from a file); medium 3; hard 4-5. Anything graded
      is at least medium. `Check: yes` for medium and hard.
- [ ] **Each Task stands alone**: exact question/key/function, files, every rule from the
      request, the traps from the brief.
- [ ] **Every Task quotes its question or docstring word for word** and holds **no answer**:
      no figures, no "state that X is absent", no expected value that is not quoted from a
      file with `file:line`. Replace any that does with the question itself.
- [ ] **Where Plan A and Plan B disagree on what a function must do, follow the docstring**
      in the brief, word for word. Keep every `Audit` subtask from either plan.
- [ ] Function names are the exact names in the brief's list of functions.
- [ ] `Depends on` only where one output needs another's answer.

## Then

Write only the plan, in **exactly** this Markdown:

```markdown
# Plan

## Deliverables
- `path` - what it must contain

## Subtask 1: <the output, in backticks exactly as the brief spells it - `q2_total`, `is_bored` - or Q3>
- Difficulty: easy | medium | hard
- Solvers: <1-5>
- Check: yes | no
- Merge: vote | best-evidence | combine | union | pick-change
- Kind: value | text | code
- Depends on: none | <subtask numbers>

### Task
<The output to produce, standing alone, with every rule that applies.>

## Assembly
<How the deliverables are built from the subtask results.>
```

---
name: investigate
description: Cross-check a finding from several angles before acting on it, so a confident guess is not mistaken for a fact. Use when being wrong would be expensive - before a refactor, when tracing a cause, when a claim will go into a document someone signs, or when the first answer looks too clean. Reports only what independent passes agree on.
---

# Being sure, as distinct from sounding sure

## The failure this exists to stop

A single pass produces one story about the evidence, and a fluent story is
indistinguishable from a correct one once it is written down. In this project that
shipped four proposed tender rows asserting **real certifications** — with nothing behind
them. Nobody was lying; one pass simply never had to disagree with itself.

## When it is worth it

`agentaus_investigate` runs several independent passes over the same question and reports
a fact only when more than one pass finds it. That costs more than a search, so spend it
where being wrong is expensive:

| Situation | Why |
| --- | --- |
| Before changing shared code | The blast radius is the thing you cannot see from one angle |
| Tracing a cause | The first plausible cause is the one that stops the search |
| A claim going into a deliverable | Someone will rely on it who cannot check it |
| The answer looks unusually clean | Real systems have exceptions; one pass tends to miss them |

Not worth it for: locating one thing (**`find-in-code`**), taking inventory
(**`repo-survey`**), or covering an "all" question (**`search-exhaustively`**).

## The script

**1. Ask one question, not three.** "Where is X and how is it called and what breaks?" is
three investigations sharing a budget; each gets a third of the attention. Ask the one
whose answer you would act on.

**2. Read the split, not just the summary.** The result separates what the passes
**agreed** on from what only one found. That division is the entire product — a summary
that flattens it is the single-pass answer again with extra steps.

**3. Confirmed and unconfirmed are different kinds of thing.**

- **Confirmed** — state it, cite `path:line`.
- **Unconfirmed** — state it *as* unconfirmed, or verify it yourself with `agentaus_zoom`
  before using it. Never promote it silently by dropping the qualifier.

**4. Disagreement is a finding.** If passes conflict, that usually means the code has two
paths, an exception, or a stale comment. Report the conflict and where it comes from.
"Two of three said X" is more useful than a clean sentence that hides it.

**5. Zoom on anything you are about to quote.** Agreement says a fact is there. It does
not give you the words.

## Rules

- Do not launch an investigation you will summarise into one sentence anyway. If a search
  would do, search.
- Do not re-investigate the same question hoping for a cleaner answer. The disagreement
  is the signal.
- Never write an unconfirmed item into a deliverable as fact. If it cannot be confirmed,
  the honest line is *"no evidence found for this"*.

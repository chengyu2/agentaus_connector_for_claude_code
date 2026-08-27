---
name: search-exhaustively
description: Cover every part of a large body of files patiently, instead of answering from the first two that matched. Use when the question says "all", "every", "the whole repo", "across the documents", "comprehensive", or when a wrong answer would come from having stopped too early. Tracks what has been covered and keeps going until two rounds find nothing new.
---

# Covering the ground, not sampling it

## The failure this exists to stop

Asked to go through a whole repository of hundreds of documents, the answer came back
built on **one or two files**. Not wrong about those files — simply blind to the rest.
The search returned a plausible hit, the hit answered the question as asked, and the
work stopped there.

That is the trap: a first hit *feels* like an answer, so the stopping condition becomes
"I have something to say" rather than "there is nothing left to find". For a question
about one thing that is fine. For a question about **all** things it is the whole bug.

## Which skill you want

- Locating **one** thing — where a behaviour lives, which file handles X → **`find-in-code`**,
  which stops early on purpose.
- Taking **inventory** of a tree first — what is even in here, how many, what formats →
  **`repo-survey`**.
- Deciding something **expensive** on what you find → **`investigate`**.
- Covering **everything** on a question that says "all" or "every" → this skill.

## The script

**1. Write down the ground before you search it.** `agentaus_inventory` on the root —
it is free and immediate, and it gives you the number. You cannot know you have finished
if you never knew what "all" was. Name it: *"41 markdown, 48 PDF, 12 docx."*

Do **not** open with a search here. Search asks each excerpt whether it answers your
question, and no single excerpt answers a question about a whole corpus — so every one
says no. Measured: two searches over a 312-file folder took 183s and 52s and returned
116 and 130 characters between them. The inventory returned all 512 files in 0.01s.

**2. Search in rounds, changing the angle each round.** One phrasing finds one
neighbourhood. Vary it deliberately:

- by **what it does** — "how the retry budget is decided"
- by **what it is called** — "backoff, retry, attempt, ceiling"
- by **who uses it** — "callers that give up after a failure"
- by **when it happens** — "what runs after a 429"

Each angle surfaces files the others miss. Running the same query again does not.

**3. Keep a covered list.** After each round write the files you have actually opened.
Not the files that matched — the files you **read**. This is the ledger that tells you
whether you are making progress or circling.

**4. Stop on two dry rounds, not on one good hit.** A round that adds no new file is one
data point. Two consecutive dry rounds, from *different angles*, is a finished search.
Anything else is a guess dressed as a conclusion.

**5. Say what you did not cover.** If you capped, skipped a format, or ran out of rounds,
state it in the answer:

> *Covered 38 of 48 PDFs; the remaining 10 are scans with no text layer.*

A silent cap reads as full coverage. It is not, and the person relying on it cannot tell.

## Rules

- Never answer an "all" question from a single search. One search is one angle.
- Never treat a truncated listing as the survey. If output was cut, you have a preview.
- Re-reading a result you already have is free. Re-running a search you already ran is
  not, and returns the same thing.
- If two rounds are dry and the answer is still thin, the honest report is *"this is
  what exists and it is thin"* — not a fuller-sounding answer built from less.

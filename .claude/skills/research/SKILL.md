---
name: research
description: Answer a question that needs information from outside this machine - current facts, standards, vendors, prices, documentation - and keep the sources attached. Use when the answer is not in the repository, when a claim needs a citation, or when asked to check what something is or what is current. Keeps web-sourced and repository-sourced claims separate.
---

# Bringing something back from outside, with its provenance intact

## Two failures this exists to stop

**Answering from memory when the question was about now.** Prices, model names, standards
and company details go stale. A confident answer with no source is unfalsifiable — the
reader cannot tell recall from research.

**Blending.** Repository facts and web facts merged into one paragraph. Later someone
asks "where did this come from" and there is no way back, because the two kinds of claim
were never separated.

## Asking well

`agentaus_web_search` performs best when the request **says so plainly**. Put the words
"web search" in the query, and expect it to take longer than a local search — that extra
time is the search actually happening.

State what you want back, not just the topic:

- Weak: `Trellis Data`
- Better: `web search for Trellis Data's published certifications and accreditations`

One well-aimed query beats three vague ones, and the vague ones each cost a wait.

## The script

**1. Decide where the answer lives before you search.** In the repo → **`find-in-code`**
or **`search-exhaustively`**. Outside it → here. Both → do the local half first; it
usually tells you what to ask the web.

**2. Search with the intent stated.** Say "web search", name the thing, and name the kind
of answer you want.

**3. Keep each claim with its origin.** As you go, not afterwards — afterwards is when
they merge:

| Claim | Source |
| --- | --- |
| Certified to ISO/IEC 27001 | web — vendor's certifications page |
| Bridge caps concurrency at 6 | repo — `gate.py:18` |

**4. Report the shape of the evidence.** *"Found on the vendor's own site, not confirmed
by an independent register"* is part of the answer. A fact and how well-attested it is are
two different things.

**5. Say when you found nothing.** *"No public source found for X"* is a real result and
often the important one — especially for a claim someone is about to put in a document.

## Rules

- Never present a web claim as a repository fact, or the reverse.
- Never state a current fact — price, version, certification, availability — from memory
  without searching. If you cannot search, say the figure is from memory and may be stale.
- Prefer the primary source. A vendor's own page beats a directory that copied it.
- Do not repeat a search that already returned something. Re-read what came back.
- If the question turns out to be answerable locally, stop and answer it locally.

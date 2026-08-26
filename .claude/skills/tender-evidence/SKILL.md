---
name: tender-evidence
description: Attach evidence from a past tender response to the claims in a new draft, row by row, without inventing support. Use when asked to strengthen, beef up, review or evidence a tender, RFP, REOI or questionnaire response against previous submissions or collateral.
---

# Evidencing a draft against a past response

## What success is, and is not

The draft is the spine. Its claims stay as written; you are finding **evidence for what it
already says**. You are not rewriting it to resemble the past response, and you are not
importing the past response's content because it sounds good.

A previous submission to a different buyer overlaps partially. On one real pairing -
a Commonwealth GovAI response against a NSW council planning tender - the overlap was real
in hosting, integration and security, and **absent** in DA workflow, GIS and ethical-AI
governance. 22 of 43 rows honestly had no supporting evidence. That was the correct result.

## The script

**1. Extract the table with its cells intact.** A `.docx` is a zip; flattening its XML with
a pattern merges cells and guesses row boundaries. Measured against LibreOffice on one
43-row table, a regex flatten lost **2,695 characters** of answer text across 12 rows -
56% of one row - and could not see the compliance column at all.

```bash
soffice --headless --convert-to html --outdir <out> "<file.docx>"
```

Then parse `<tr>`/`<td>`. Requirement and response often share one cell, split by a
"Vendor Response:" label rather than by markup.

**2. Confirm the extraction before using it.** Count the rows. Name any row with no
substantive answer - a cross-reference like *"see Q20 in Section 6"* is not an answer, and
an **Essential** row with only a cross-reference is worth flagging to a human.

**3. Work in small batches.** Three or four rows per turn. A single turn asked for nine
delivered one. Batch by section so one search serves the whole batch.

**4. Search the reference corpus once per batch**, then `agentaus_zoom` the citations you
intend to quote. A search hit proves a fact exists; it is not enough to quote from.

**5. Emit one block per row, in the draft's own order:**

```
### <ref>
**Evidence:** "<verbatim quote>" (<file>:<line>)   or exactly: no evidence found
**Suggested addition:** <ONE sentence to append to the draft, carrying only what the
quote supports>                                     or exactly: nothing to add
**Buyer fit:** <one line: does this evidence suit THIS buyer, or read as padding from
the other one?>
```

## Rules

- **No evidence means nothing to add.** If Evidence is "no evidence found" then Suggested
  addition must be exactly "nothing to add". This is the rule that gets broken: in one run
  four rows of 43 proposed additions asserting ISO 27001 and Essential Eight compliance
  with no quote behind them. Real certifications, invented support - which is the worst
  combination, because it reads as verified.
- **Never replace a buyer-specific claim with one from the other tender.** A council
  planning buyer does not want Commonwealth framing.
- **Quote verbatim, cite file and line.** A paraphrase cannot be checked.
- **A web citation is weaker than a submitted response.** If a quote comes from a public
  page rather than the reference corpus, label it and expect it to be verified.
- Say how many rows had no evidence. That number is the useful finding, not a failure.

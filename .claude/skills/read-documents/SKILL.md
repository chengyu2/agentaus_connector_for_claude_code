---
name: read-documents
description: Read Word documents, spreadsheets, presentations and PDFs correctly, keeping tables intact. Use when a question involves a .docx, .xlsx, .pptx or .pdf, when a document read comes back as binary noise or suspiciously short, or before extracting a table from any office file.
---

# Reading office documents without losing their tables

## Why not just read the file

A `.docx`, `.xlsx` and `.pptx` are zip archives. Read as text they are binary noise. A PDF
is worse: it will happily return megabytes of nonsense that looks like content.

The bridge converts them with LibreOffice automatically, so `agentaus_search` and
`agentaus_zoom` see them as text with **table rows on one line, cells separated by ` | `**.
That is deliberate: one line per row means a line number still identifies a row, so a
citation into a spreadsheet works exactly as it does into source code.

## When you need to do it yourself

```bash
soffice --headless --convert-to html --outdir <out> "<file.docx>"
```

HTML rather than txt, because it keeps `<table>`, `<tr>` and `<td>`.

**Do not flatten the XML with a pattern instead.** Measured against LibreOffice on one real
43-row requirements table, a regex flatten:

- lost **2,695 characters** of answer text across 12 rows — 56% of one row
- could not see the **compliance column at all**, having no idea where a cell ended
- guessed at row boundaries, so the last row **swallowed 77,000 characters** of the document

None of that raises an error. It produces slightly wrong input and confidently wrong output,
and one truncated row made an evidence review come back empty for hours while the failure
was blamed on the upstream.

## PDFs

Text-layer PDFs convert. **Image-only PDFs do not** — scans, certificates, anything
exported as pictures. LibreOffice returns a page of CSS and a heap of GIFs, which is not
the document.

When a PDF yields almost nothing, say so. Do not describe what a certificate probably says.
An unreadable document named as unreadable is useful; one guessed at is not.

## Rules

- Convert once, then work from the text. Re-converting per question is slow.
- Check the extraction before trusting it: count the rows, and check the last row is not
  absurdly larger than the rest — that is the signature of a boundary that never closed.
- A row is one line. If a row spans lines, line-number citations stop meaning anything.
- Say which tool read the document. "LibreOffice extracted 137,607 characters" is checkable.

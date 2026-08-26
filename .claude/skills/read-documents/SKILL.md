---
name: read-documents
description: Read Word documents, spreadsheets, presentations and PDFs correctly, keeping tables intact. Use when a question involves a .docx, .xlsx, .pptx or .pdf, when a document read comes back as binary noise or suspiciously short, or before extracting a table from any office file.
---

# Reading office documents without losing their tables

## Why not just read the file

A `.docx`, `.xlsx` and `.pptx` are zip archives. Read as text they are binary noise. A PDF
is worse: read as text it returns megabytes of nonsense that looks like content.

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

PDFs do **not** go through LibreOffice. Its importer opens a PDF as a drawing: one real
tender PDF came back as 112 characters of stylesheet and a hundred GIFs, which reads as
"this document is empty" when it was eight pages of text.

They go through a ladder instead — several extractors tried in order, then OCR on any
page that still came back blank or garbled. Scanned pages and image-only certificates are
readable. Across the 48 PDFs this was built against, all 48 extract.

Two things follow for you:

- **Pages are numbered.** Extracted text carries `[page N]` markers. Cite them — a page is
  the only coordinate a PDF has, and a quote from a 93-page tender cannot be checked
  without one.
- **Read the log line if something looks thin.** It names the tier that won and how many
  pages needed OCR. A PDF that yields almost nothing after all that is genuinely close to
  empty — say so rather than describing what a certificate probably says. An unreadable
  document named as unreadable is useful; one guessed at is not.

## Rules

- Convert once, then work from the text. Re-converting per question is slow.
- Check the extraction before trusting it: count the rows, and check the last row is not
  absurdly larger than the rest — that is the signature of a boundary that never closed.
- A row is one line. If a row spans lines, line-number citations stop meaning anything.
- Say which tool read the document. "LibreOffice extracted 137,607 characters" is checkable.

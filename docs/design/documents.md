# Reading documents

How the bridge reads office documents and PDFs, and why it reads them this way. The README's
[Word documents and spreadsheets](../../README.md#word-documents-and-spreadsheets) and
[PDFs](../../README.md#pdfs) sections are the short version.

## Word documents and spreadsheets

A `.docx` is a zip archive, so the bridge used to walk past office documents as binary.
That meant `agentaus_search` and `agentaus_zoom` could not see a tender response, a
requirements matrix or a specification — which is exactly where that material lives.

They are read by default now, converted with **LibreOffice**, which knows the format.
LibreOffice is an [optional reader](../../README.md#requirements); `soffice` is found in the usual places
or on `$PATH`, `AGENTAUS_SOFFICE_PATH` overrides it, and `AGENTAUS_OFFICE_EXTRACT=false`
goes back to skipping them.

**Tables survive, and that is the point.** A row becomes one line with ` | ` between
cells:

```
   79  5.3
   80  Security and Governance
   81  Reference | Requirement | Compliance
   85  5.3.4 | Essential: Model governance documentation... | Yes
```

One line per row means a line number still identifies a row, so `agentaus_zoom` cites into
a spreadsheet exactly as it cites into source code.

### One reader, on purpose

If LibreOffice is absent, the bridge logs a warning at startup and skips office documents.
It does not fall back to a lighter library.

`python-docx` and `openpyxl` would handle `.docx` and `.xlsx` in 496 KB against
LibreOffice's 795 MB, so the temptation is obvious. They are also a **second
implementation of "how does a table become text"** — different format coverage, and a
different rendering of the same document. Two answers to that question is worse than one
large dependency: a row that reads one way on a laptop and another way in CI is a bug
nobody finds until a number is wrong in a tender response.

```
macOS   brew install --cask libreoffice
Debian  sudo apt install libreoffice-writer libreoffice-calc
```

Nothing to configure afterwards — `soffice` is found on `$PATH` or in the usual
locations, and `AGENTAUS_SOFFICE_PATH` covers an unusual one.

### Why not just strip the XML

Because it fails quietly. Measured against LibreOffice on one real 43-row requirements
table, a regex flatten of the document XML:

- lost **2,695 characters** of answer text across 12 rows — **56% of one row**, whose
  evidence review then came back empty and looked like an upstream fault
- could not see the **compliance column at all**, having no idea where one cell ended and
  the next began
- guessed at row boundaries, so the final row **swallowed 77,000 characters** of the rest
  of the document

None of that raises an error. It just produces slightly wrong input, which produces
confidently wrong output.

Conversions are cached per file, keyed on path, mtime and size — so a search reading many
chunks converts once, and a document edited under the bridge is re-read rather than served
stale. Each conversion gets a private LibreOffice profile, because concurrent `soffice`
invocations otherwise fight over the shared one and a run silently produces nothing.

---

## PDFs

Every PDF used to be skipped outright. `.pdf` sat in the search tool's binary-suffix
list, so `enumerate_files` returned nothing for one and `read_text` returned megabytes
of noise. In the corpus this was built against that hid **48 documents** — which is a
large part of why a question about "the whole repo" came back answered from two files.

They do **not** go through LibreOffice. Its importer opens a PDF as a drawing to be
edited: converting one real eight-page document produced 112 characters of stylesheet and
a hundred GIFs. I read that as "this PDF is an image-only scan" and was wrong — it was a
conclusion about LibreOffice's PDF filter mistaken for a conclusion about the file.
`pypdf` reads the same document fine.

So PDFs get a ladder instead. Extractors are tried in order, each result is checked **per
page**, and only the pages that came back blank or garbled are escalated to OCR — a
twenty-page report with two scanned pages costs two OCR calls, not twenty.

| Tier | Needs | Why it is where it is |
| --- | --- | --- |
| `pdftotext -layout` | `brew install poppler` | Won on **all 48** files, often by 2–3×, and ran 10–30× faster. `-layout` keeps the column and table geometry the others flatten |
| `pdfminer.six` | pip | Pure Python fallback. Slower, loses columns |
| `pypdf` | pip | Last resort. Fastest of the two, extracts least |
| **macOS Vision** | pyobjc | Neural OCR built into the OS. No model download, ~0.5s/page, rasterises through Quartz so it adds no imaging dependency |
| **tesseract** | `brew install tesseract` | Cross-platform OCR tier |

poppler and tesseract are [optional](../../README.md#requirements): without them the bridge starts,
logs a warning naming each missing reader, and the ladder starts at the Python tiers. It
also degrades at run time - a tier that fails on one file hands it to the next.

### One bug worth recording

A form feed *separates* pages and is also emitted after the last one, so splitting on it
invents an empty final page. That phantom page failed the readability gate, which
disqualified the best extractor on most of the corpus and silently handed the job to the
worst — the failure looked like success, because text still came back. Fixing it took one
tender PDF from 17,771 characters to 23,344.

All 48 now extract: **1,120,798 characters in 11 seconds.**

Extracted text carries `[page N]` markers, because a page number is the only coordinate a
PDF has and a quote from a 93-page tender cannot be checked without one.

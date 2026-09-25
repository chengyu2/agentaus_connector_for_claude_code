"""What the bridge reads out of a document must be the document, and nothing else.

The failure this guards against was silent. LibreOffice writes a <style> block into the
HTML it converts to, and the bridge kept it: every .docx, .xlsx and .pptx it read began
with 18 lines of CSS - 1,845 characters around 150 of content in a three-row table - and
that went into every search chunk, citation and inventory headline. Nothing failed; the
model just read stylesheet before it read the tender.

A unit test on a hand-written HTML sample would not have caught it, because the sample
would have had no stylesheet. So these build real documents with the same LibreOffice
the bridge uses, read them through the bridge's own extraction path, and check two
things: every known value comes out, and no noise signature does. A future LibreOffice
or poppler that starts leaking something new fails here rather than in a transcript.

Skipped when LibreOffice is not installed, as the bridge itself would then skip office
documents.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import documents, pdf  # noqa: E402

CELLS = [("Ref", "Requirement", "Compliance"),
         ("R1", "Data stored in Australia", "Yes"),
         ("R2", "SSO via SAML 2.0", "Partial"),
         ("R3", "Uptime of 99.9 percent", "Yes")]

# What noise has looked like, or plausibly would: stylesheet rules, markup, page setup,
# control characters, the Unicode replacement character that binary decodes into.
NOISE = [
    ("a CSS rule", re.compile(r"^\s*[@\w.#:,\s>*-]+\{[^}]*\}\s*$", re.M)),
    ("an HTML tag", re.compile(r"</?(?:html|head|body|style|script|meta|link|div|span|p|table|tr|td|th)\b[^>]*>", re.I)),
    ("page setup", re.compile(r"@page|size:\s*\d|margin-(?:left|top)", re.I)),
    ("a control character", re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")),
    ("decoded binary", re.compile("�")),
]


def _html():
    rows = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in CELLS)
    return (f"<html><head><style>p {{ color: #000 }}</style></head><body>"
            f"<h1>Requirements</h1><table border='1'>{rows}</table></body></html>")


def _convert(src: str, fmt: str, outdir: str) -> str:
    """Convert with LibreOffice into `outdir`; return the produced file's path."""
    profile = os.path.join(outdir, "profile")
    subprocess.run([documents.soffice(), f"-env:UserInstallation=file://{profile}",
                    "--headless", "--norestore", "--convert-to", fmt, "--outdir", outdir, src],
                   capture_output=True, timeout=180, check=False)
    stem = os.path.splitext(os.path.basename(src))[0]
    ext = fmt.split(":")[0]
    return os.path.join(outdir, f"{stem}.{ext}")


@unittest.skipUnless(documents.soffice(), "LibreOffice is not installed")
class RealDocumentsReadClean(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        d = cls.dir.name
        html = os.path.join(d, "requirements.html")
        with open(html, "w") as fh:
            fh.write(_html())
        csv = os.path.join(d, "matrix.csv")
        with open(csv, "w") as fh:
            fh.write("\n".join(",".join(row) for row in CELLS) + "\n")
        cls.files = {
            "docx": _convert(html, "docx:MS Word 2007 XML", d),
            "odt": _convert(html, "odt", d),
            "xlsx": _convert(csv, "xlsx", d),
            "pdf": _convert(html, "pdf", d),
        }
        documents.reset_cache()

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def _read(self, kind):
        path = self.files[kind]
        if not os.path.exists(path):
            self.skipTest(f"LibreOffice could not produce a {kind} here")
        if kind == "pdf" and not pdf.available():
            self.skipTest("no PDF extractor installed")
        return documents.extract(path)

    def _check(self, kind):
        text = self._read(kind)
        self.assertTrue(text.strip(), f"{kind}: nothing extracted")
        for label, pattern in NOISE:
            hit = pattern.search(text)
            self.assertIsNone(hit, f"{kind}: extracted text contains {label}: "
                                   f"{hit.group(0)[:80]!r}" if hit else "")
        flat = " ".join(text.split())
        for row in CELLS:
            for cell in row:
                self.assertIn(cell, flat, f"{kind}: lost the value {cell!r}")
        # Content, not preamble, comes first - the stylesheet bug put CSS on line one.
        first = next(line for line in text.splitlines() if line.strip())
        self.assertTrue(any(v in first for v in ("Requirements", "Ref", "R1", "[page 1]")),
                        f"{kind}: first line is not content: {first[:80]!r}")
        return text

    def test_word_document(self):
        text = self._check("docx")
        self.assertIn("R2 | SSO via SAML 2.0 | Partial", text, "table rows must stay on one line")

    def test_opendocument_text(self):
        self._check("odt")

    def test_spreadsheet(self):
        self._check("xlsx")

    def test_pdf(self):
        self._check("pdf")


class TheNoiseCheckItselfWorks(unittest.TestCase):
    """A guard that cannot fail protects nothing: each signature must fire on its noise."""

    def test_each_signature_catches_what_it_names(self):
        samples = {
            "a CSS rule": "p.ctl { font-family: Arial }",
            "an HTML tag": "<td>R1</td>",
            "page setup": "@page { size: 21cm 29.7cm }",
            "a control character": "R1\x07Yes",
            "decoded binary": "R1 �� Yes",
        }
        for label, pattern in NOISE:
            with self.subTest(signature=label):
                self.assertIsNotNone(pattern.search(samples[label]))

    def test_clean_text_passes(self):
        clean = "Requirements\nRef | Requirement | Compliance\nR1 | Data stored in Australia | Yes"
        for label, pattern in NOISE:
            with self.subTest(signature=label):
                self.assertIsNone(pattern.search(clean))


if __name__ == "__main__":
    unittest.main()

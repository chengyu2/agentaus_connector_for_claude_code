"""Reading Word documents and spreadsheets with their tables intact.

A .docx is a zip archive, so the bridge used to skip office documents as binary - which
meant search and zoom could not see a tender response or a requirements matrix, which is
where that material lives. Flattening the XML with a pattern is not good enough either,
and it fails quietly: measured against LibreOffice on one real 43-row table, a regex
flatten lost 2,695 characters across 12 rows, missed the compliance column entirely, and
let the final row swallow 77,000 characters of the rest of the document.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import documents, tools  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402


class TestRecognition(unittest.TestCase):
    def test_office_formats_are_recognised(self):
        for name in ("a.docx", "b.DOC", "c.xlsx", "d.ods", "e.pptx", "f.rtf"):
            self.assertTrue(documents.is_office_document(name), name)

    def test_other_files_are_not(self):
        for name in ("a.py", "b.md", "c.json", "d.png", "e.sqlite"):
            self.assertFalse(documents.is_office_document(name), name)


class TestHtmlRendering(unittest.TestCase):
    """The conversion keeps <table>; this turns it back into text without losing cells."""

    def test_a_table_row_becomes_one_line_with_cells_separated(self):
        html = ("<html><body><p>Intro para</p>"
                "<table><tr><td>5.1.1</td><td>Essential: do the thing</td><td>Yes</td></tr>"
                "<tr><td>5.1.2</td><td>Desirable: do another</td><td>No</td></tr>"
                "</table><p>After the table</p></body></html>")
        text = documents.html_to_text(html)
        lines = [l for l in text.splitlines() if l.strip()]
        self.assertIn("Intro para", lines)
        self.assertIn("5.1.1 | Essential: do the thing | Yes", lines)
        self.assertIn("5.1.2 | Desirable: do another | No", lines)
        self.assertIn("After the table", lines)

    def test_a_row_stays_on_one_line_so_line_numbers_identify_it(self):
        """agentaus_zoom cites by line, so a row split across lines is uncitable."""
        html = ("<table><tr><td>ref</td>"
                "<td><p>first para</p><p>second para</p></td></tr></table>")
        rows = [l for l in documents.html_to_text(html).splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertIn("first para second para", rows[0])

    def test_entities_are_decoded(self):
        html = "<table><tr><td>a &amp; b</td><td>&lt;tag&gt;</td></tr></table>"
        self.assertIn("a & b | <tag>", documents.html_to_text(html))

    def test_empty_rows_are_dropped(self):
        html = "<table><tr><td></td><td></td></tr><tr><td>real</td></tr></table>"
        rows = [l for l in documents.html_to_text(html).splitlines() if l.strip()]
        self.assertEqual(rows, ["real"])

    def test_header_cells_count_as_cells(self):
        html = "<table><tr><th>Ref</th><th>Requirement</th></tr></table>"
        self.assertIn("Ref | Requirement", documents.html_to_text(html))


class TestFallbackWhenLibreOfficeIsMissing(unittest.TestCase):
    def setUp(self):
        self._saved = settings.agentaus_office_extract
        documents.reset_cache()

    def tearDown(self):
        settings.agentaus_office_extract = self._saved
        documents.reset_cache()

    def test_extraction_off_means_office_files_are_skipped_again(self):
        settings.agentaus_office_extract = False
        self.assertFalse(documents.available())
        self.assertIn(".docx", tools._skipped_suffixes())

    def test_extraction_on_means_they_are_enumerated(self):
        settings.agentaus_office_extract = True
        if not documents.available():
            self.skipTest("LibreOffice is not installed here")
        self.assertNotIn(".docx", tools._skipped_suffixes())

    def test_a_document_that_cannot_be_converted_never_raises(self):
        """A bad document must look like a document with little in it, not fail the turn.

        LibreOffice is forgiving and will often salvage something from a mislabelled
        file, so this asserts the contract - a string, no exception - rather than a
        particular result.
        """
        settings.agentaus_office_extract = True
        if not documents.available():
            self.skipTest("LibreOffice is not installed here")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            broken = os.path.join(d, "broken.docx")
            with open(broken, "w") as fh:
                fh.write("this is not a zip archive")
            self.assertIsInstance(documents.extract(broken), str)

    def test_a_missing_file_reads_as_empty(self):
        settings.agentaus_office_extract = True
        if not documents.available():
            self.skipTest("LibreOffice is not installed here")
        self.assertEqual(documents.extract("/nonexistent/x.docx"), "")


class TestCaching(unittest.TestCase):
    def setUp(self):
        documents.reset_cache()

    def test_the_cache_key_changes_when_the_file_does(self):
        import tempfile, time
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.docx")
            with open(path, "w") as fh:
                fh.write("one")
            first = documents._key(path)
            time.sleep(0.01)
            with open(path, "w") as fh:
                fh.write("one but longer")
            self.assertNotEqual(first, documents._key(path),
                                "a changed document would have served a stale extract")


if __name__ == "__main__":
    unittest.main(verbosity=2)

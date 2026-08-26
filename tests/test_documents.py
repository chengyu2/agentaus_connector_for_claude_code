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


class TestRepairingClientSideReads(unittest.TestCase):
    """Claude Code's `Read` runs on the client, so the bridge cannot change what it does.

    What it does with a `.docx` is hand back zip noise, because a `.docx` is a zip. The
    bridge knows which file the call named and runs on the same machine, so it can fix
    the result before anything else reads it.
    """

    def setUp(self):
        documents.reset_cache()
        if not documents.available():
            self.skipTest("LibreOffice is not installed here")

    def _conversation(self, path, content, is_error=False, tool="Read", field="file_path"):
        result = {"type": "tool_result", "tool_use_id": "t1", "content": content}
        if is_error:
            result["is_error"] = True
        return {"messages": [
            {"role": "user", "content": "read it"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": tool, "input": {field: path}}]},
            {"role": "user", "content": [result]},
        ]}

    def _docx(self, directory):
        """A real .docx, built by LibreOffice so the test does not ship a binary."""
        import subprocess
        src = os.path.join(directory, "src.txt")
        with open(src, "w") as fh:
            fh.write("Heading\nA line of real content\n")
        subprocess.run([documents.soffice(),
                        f"-env:UserInstallation=file://{directory}/profile",
                        "--headless", "--convert-to", "docx", "--outdir", directory, src],
                       capture_output=True, timeout=120)
        return os.path.join(directory, "src.docx")

    def test_zip_noise_is_replaced_with_the_document_text(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            docx = self._docx(d)
            if not os.path.exists(docx):
                self.skipTest("could not build a .docx fixture")
            body = self._conversation(docx, "PK\x03\x04\x14\x00binary noise")
            fixed = documents.repair_tool_results(body)
            text = fixed["messages"][2]["content"][0]["content"]
            self.assertIn("A line of real content", text)
            self.assertIn("LibreOffice", text, "it should say why the result changed")

    def test_a_non_office_read_is_left_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.py")
            with open(path, "w") as fh:
                fh.write("x = 1\n")
            body = self._conversation(path, "x = 1")
            self.assertIs(documents.repair_tool_results(body), body)

    def test_an_error_result_is_left_alone(self):
        """Its exact text is what is being debugged."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            docx = self._docx(d)
            if not os.path.exists(docx):
                self.skipTest("could not build a .docx fixture")
            body = self._conversation(docx, "permission denied", is_error=True)
            self.assertIs(documents.repair_tool_results(body), body)

    def test_a_missing_document_leaves_the_result_untouched(self):
        body = self._conversation("/nonexistent/report.docx", "PK\x03\x04")
        self.assertIs(documents.repair_tool_results(body), body)

    def test_it_is_a_no_op_when_extraction_is_off(self):
        previous = settings.agentaus_office_extract
        settings.agentaus_office_extract = False
        try:
            body = self._conversation("/some/report.docx", "PK\x03\x04")
            self.assertIs(documents.repair_tool_results(body), body)
        finally:
            settings.agentaus_office_extract = previous

    def test_other_path_field_names_are_recognised(self):
        """Not every tool calls it file_path - MCP servers and others vary."""
        self.assertEqual(documents._named_path({"path": "/a/b.docx"}), "/a/b.docx")
        self.assertEqual(documents._named_path({"filename": "/a/b.xlsx"}), "/a/b.xlsx")
        self.assertIsNone(documents._named_path({"query": "not a path"}))
        self.assertIsNone(documents._named_path("not a dict"))

"""The PDF ladder: tier order, the readability gate, and where OCR is spent."""

import logging
import os
import tempfile
import unittest

from agentaus_bridge import documents, pdf
from agentaus_bridge.config import settings


READABLE = "The quick brown fox jumps over the lazy dog, repeatedly and at length. " * 3
FEED = "\x0c"


class Pages(unittest.TestCase):
    def test_trailing_form_feed_does_not_become_a_page(self):
        """The bug that handed the job to the worst extractor on most of the corpus.

        A form feed separates pages and is also emitted after the last one, so a naive
        split invents an empty final page. That page fails the readability gate, which
        disqualified pdftotext - the best tier - on nearly every real file.
        """
        self.assertEqual(pdf._pages("one" + FEED + "two" + FEED), ["one", "two"])
        self.assertEqual(pdf._pages("only"), ["only"])
        self.assertEqual(pdf._pages(""), [])

    def test_pages_are_tagged_so_a_quote_can_be_cited(self):
        assembled = pdf._assemble(["first page text", "second page text"])
        self.assertIn("[page 1]", assembled)
        self.assertIn("[page 2]", assembled)

    def test_a_single_page_is_not_tagged(self):
        self.assertEqual(pdf._assemble(["just this"]), "just this")

    def test_blank_pages_are_dropped_from_the_assembly(self):
        assembled = pdf._assemble(["real text here", "   ", "more real text"])
        self.assertNotIn("[page 2]", assembled)


class ReadabilityGate(unittest.TestCase):
    def test_ordinary_prose_is_readable(self):
        self.assertTrue(pdf.page_is_readable(READABLE))

    def test_a_page_with_almost_nothing_is_not(self):
        self.assertFalse(pdf.page_is_readable("TITLE"))

    def test_mojibake_is_not_readable_despite_its_length(self):
        """A broken CID map yields plenty of characters and no language."""
        self.assertFalse(pdf.page_is_readable("�一二" * 200))

    def test_empty_is_not_readable(self):
        self.assertFalse(pdf.page_is_readable(""))
        self.assertFalse(pdf.page_is_readable(None))


class Ladder(unittest.TestCase):
    def setUp(self):
        self.tiers, self.ocr = pdf.TEXT_TIERS, pdf.OCR_TIERS
        self.path = os.path.join(tempfile.gettempdir(), "ladder.pdf")

    def tearDown(self):
        pdf.TEXT_TIERS, pdf.OCR_TIERS = self.tiers, self.ocr

    def test_the_first_fully_readable_tier_wins_and_later_tiers_do_not_run(self):
        ran = []

        def best(path):
            ran.append("best")
            return [READABLE]

        def worse(path):
            ran.append("worse")
            return [READABLE + "extra"]

        pdf.TEXT_TIERS = (("best", best), ("worse", worse))
        self.assertIn("quick brown fox", pdf.extract(self.path))
        self.assertEqual(ran, ["best"])

    def test_a_missing_library_is_skipped_rather_than_failing_the_read(self):
        pdf.TEXT_TIERS = (("absent", lambda p: None), ("present", lambda p: [READABLE]))
        self.assertIn("quick brown fox", pdf.extract(self.path))

    def test_when_no_tier_is_clean_the_one_with_the_most_text_is_kept(self):
        pdf.TEXT_TIERS = (
            ("thin", lambda p: [READABLE, ""]),
            ("fuller", lambda p: [READABLE, READABLE, ""]),
        )
        pdf.OCR_TIERS = (("none", lambda p, w: {}),)
        self.assertEqual(pdf.extract(self.path).count("quick brown fox"), 6)

    def test_ocr_runs_only_on_the_pages_that_failed(self):
        asked = []

        def ocr(path, wanted):
            asked.append(list(wanted))
            return {index: READABLE for index in wanted}

        pdf.TEXT_TIERS = (("t", lambda p: [READABLE, "", READABLE, "x"]),)
        pdf.OCR_TIERS = (("stub", ocr),)
        pdf.extract(self.path)
        self.assertEqual(asked, [[1, 3]], "OCR must not be spent on readable pages")

    def test_ocr_output_is_rejected_when_it_is_worse_than_what_it_replaces(self):
        pdf.TEXT_TIERS = (("t", lambda p: [READABLE, "short but present"]),)
        pdf.OCR_TIERS = (("stub", lambda p, w: {1: "x"}),)
        self.assertIn("short but present", pdf.extract(self.path))

    def test_the_ocr_page_cap_is_announced_rather_than_applied_silently(self):
        pdf.TEXT_TIERS = (("t", lambda p: [""] * 10),)
        pdf.OCR_TIERS = (("stub", lambda p, w: {i: READABLE for i in w}),)
        original = settings.agentaus_pdf_ocr_max_pages
        settings.agentaus_pdf_ocr_max_pages = 3
        try:
            with self.assertLogs("agentaus-bridge", level=logging.WARNING) as caught:
                pdf.extract(self.path)
            self.assertTrue(any("first 3" in line for line in caught.output))
        finally:
            settings.agentaus_pdf_ocr_max_pages = original

    def test_a_pdf_nothing_can_read_yields_no_text_rather_than_an_exception(self):
        pdf.TEXT_TIERS = (("nothing", lambda p: None),)
        self.assertEqual(pdf.extract(self.path), "")


class Routing(unittest.TestCase):
    def test_documents_sends_pdfs_to_the_ladder_not_to_libreoffice(self):
        """LibreOffice opens a PDF as a drawing and returns CSS where text should be."""
        self.assertTrue(documents.is_pdf("/x/report.PDF"))
        self.assertTrue(documents.is_office_document("/x/report.pdf"))
        self.assertNotIn(".pdf", documents.OFFICE_SUFFIXES)

    def test_a_document_is_not_measured_by_its_size_on_disk(self):
        """27 documents in one corpus were excluded for being "too large".

        Among them a 93-page tender response: 1.3 MB on disk, 244,805 characters of
        text, and the only file containing the identifier being searched for. A .pptx
        of 16.7 MB held a few thousand characters. Size on disk is embedded images, not
        content, so it is the wrong quantity to cap these on.
        """
        import os
        import tempfile

        from agentaus_bridge import tools
        from agentaus_bridge.config import settings

        with tempfile.TemporaryDirectory() as root:
            big = settings.agentaus_search_max_file_bytes + 4096
            for name in ("report.pdf", "deck.pptx", "notes.md", "module.py"):
                with open(os.path.join(root, name), "wb") as handle:
                    handle.write(b"x" * big)

            found = {os.path.basename(f) for f in tools.enumerate_files(root)}
            self.assertIn("report.pdf", found, "an oversized PDF must still be a candidate")
            self.assertIn("deck.pptx", found)
            self.assertNotIn("notes.md", found, "a 1MB text file really is too large")
            self.assertNotIn("module.py", found)

    def test_documents_are_still_capped_somewhere(self):
        """Raised, not removed. A genuinely enormous file is still skipped."""
        import os
        import tempfile

        from agentaus_bridge import tools
        from agentaus_bridge.config import settings

        original = settings.agentaus_search_max_document_bytes
        settings.agentaus_search_max_document_bytes = 2048
        try:
            with tempfile.TemporaryDirectory() as root:
                with open(os.path.join(root, "huge.pdf"), "wb") as handle:
                    handle.write(b"x" * 8192)
                found = {os.path.basename(f) for f in tools.enumerate_files(root)}
                self.assertNotIn("huge.pdf", found)
        finally:
            settings.agentaus_search_max_document_bytes = original

    def test_availability_is_asked_per_format(self):
        """poppler without LibreOffice reads PDFs and not .docx, and vice versa."""
        self.assertIsInstance(documents.available("/x/a.pdf"), bool)
        self.assertIsInstance(documents.available("/x/a.docx"), bool)


if __name__ == "__main__":
    unittest.main()

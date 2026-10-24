"""Every document reader is required: the bridge names what is missing and will not start."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import __main__ as entry  # noqa: E402
from agentaus_bridge import documents, pdf, requirements  # noqa: E402


class Missing(unittest.TestCase):
    def test_nothing_missing_when_every_reader_is_there(self):
        with mock.patch.object(documents, "soffice", return_value="/x/soffice"), \
                mock.patch.object(pdf, "_tool", return_value="/x/tool"), \
                mock.patch.object(requirements, "_module", return_value=True):
            self.assertEqual(requirements.missing(), [])

    def test_each_missing_reader_is_named_with_its_install_command(self):
        with mock.patch.object(documents, "soffice", return_value=None), \
                mock.patch.object(pdf, "_tool", side_effect=lambda n: None if n == "tesseract" else "/x/" + n), \
                mock.patch.object(requirements, "_module", side_effect=lambda m: m != "pdfminer.high_level"):
            lines = requirements.missing()
        self.assertEqual(len(lines), 3)
        self.assertTrue(any(l.startswith("LibreOffice") and "libreoffice" in l for l in lines))
        self.assertTrue(any(l.startswith("tesseract") and "install tesseract" in l.replace("tesseract-ocr", "tesseract") for l in lines))
        self.assertTrue(any(l.startswith("pdfminer.six") and "requirements.txt" in l for l in lines))
        self.assertIn("install.sh", requirements.report(lines))

    def test_the_bridge_refuses_to_start_without_them(self):
        from agentaus_bridge.config import settings
        with mock.patch.object(settings, "agentaus_api_key", "k"), \
                mock.patch.object(requirements, "missing", return_value=["LibreOffice (`soffice`)"]), \
                mock.patch("uvicorn.run") as run, \
                self.assertLogs("agentaus-bridge", level="ERROR") as caught:
            code = entry.main(["--env-file", "/nonexistent/.env"])
        self.assertEqual(code, 3)
        run.assert_not_called()
        self.assertTrue(any("LibreOffice" in line for line in caught.output))


class RealReaders(unittest.TestCase):
    """The installed tools themselves, not stubs: they are required, so they are tested.
    Measured: on poppler 26.10 `pdftoppm ... -` wrote a file named "-.png" instead of
    stdout, and the tesseract tier returned nothing for every page while its stub test
    passed."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        cls.dir = tempfile.mkdtemp(prefix="readers-")
        src = os.path.join(cls.dir, "page.txt")
        with open(src, "w") as fh:
            fh.write("The quick brown fox jumps over the lazy dog. Invoice 4471.")
        subprocess.run([documents.soffice(), "--headless", "--convert-to", "pdf", "--outdir", cls.dir, src],
                       capture_output=True, timeout=180)
        cls.pdf = os.path.join(cls.dir, "page.pdf")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_every_requirement_is_installed(self):
        self.assertEqual(requirements.missing(), [], "run ./scripts/install.sh")

    def test_libreoffice_made_a_pdf_and_each_ocr_tier_reads_it(self):
        self.assertTrue(os.path.exists(self.pdf), "LibreOffice did not convert")
        for name, run in pdf.OCR_TIERS:
            if name == "vision" and not requirements.MAC:
                continue
            self.assertIn("Invoice 4471", run(self.pdf, [0]).get(0, ""), name)

    def test_the_text_tiers_read_it(self):
        for name, run in pdf.TEXT_TIERS:
            self.assertIn("quick brown fox", "".join(run(self.pdf) or []), name)


if __name__ == "__main__":
    unittest.main()

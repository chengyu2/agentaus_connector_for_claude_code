"""Only the core packages are required. A component chosen at install that is missing is a
warning naming its install command; one the user skipped is a single info line."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import __main__ as entry  # noqa: E402
from agentaus_bridge import components, documents, pdf, requirements  # noqa: E402


class Missing(unittest.TestCase):
    def test_nothing_missing_when_every_reader_is_there(self):
        with mock.patch.object(components, "_binary", return_value="/x/tool"), \
                mock.patch.object(components, "_module", return_value=True):
            self.assertEqual(requirements.missing(), [])
            self.assertEqual(requirements.optional_missing(), [])
            self.assertEqual(requirements.degraded(), [])

    def test_only_core_packages_are_required(self):
        with mock.patch.object(components, "_module", side_effect=lambda m: m not in ("httpx", "pypdf")):
            lines = requirements.missing()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("httpx") and "requirements/core.txt" in lines[0])
        self.assertIn("install.sh", requirements.report(lines))

    def test_a_chosen_component_that_is_missing_is_named_with_its_install_command(self):
        with mock.patch.object(requirements, "WIN", False), mock.patch.object(requirements, "MAC", False), \
                mock.patch.object(components, "package_manager", return_value="apt"), \
                mock.patch.object(components, "_binary", side_effect=lambda n: None if n in ("soffice", "tesseract") else "/x/" + n), \
                mock.patch.object(components, "_module", side_effect=lambda m: m != "pdfminer.high_level"):
            lines = requirements.degraded(["core", "pdf", "poppler", "ocr", "office", "outline"])
            report = requirements.degraded_report(lines)
        self.assertEqual(len(lines), 3)
        self.assertTrue(any(l.startswith("PDF text fallbacks") and "requirements/pdf.txt" in l for l in lines))
        self.assertTrue(any(l.startswith("Office documents") and "sudo apt install libreoffice-writer" in l for l in lines))
        self.assertTrue(any(l.startswith("OCR") and "tesseract-ocr" in l for l in lines))
        self.assertIn("agentaus-bridge components", report)

    def test_a_skipped_component_is_not_degraded_only_skipped(self):
        with mock.patch.object(requirements, "MAC", False), mock.patch.object(requirements, "WIN", False), \
                mock.patch.object(components, "_binary", return_value=None), \
                mock.patch.object(components, "_module", return_value=True):
            chosen = ["core", "pdf", "outline"]
            self.assertEqual(requirements.degraded(chosen), [])
            skipped = requirements.skipped(chosen)
        self.assertIn("Office documents (LibreOffice)", skipped)
        self.assertIn("OCR for scanned PDFs (tesseract)", skipped)
        self.assertNotIn("Claude Code settings", skipped)

    def test_an_install_without_components_counts_as_choosing_everything(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(components.ENV_KEY, None)
            self.assertEqual(components.selected(), [c.key for c in components.available()])

    def test_windows_hints_are_winget_and_powershell(self):
        with mock.patch.object(requirements, "WIN", True), \
                mock.patch.object(components, "package_manager", return_value="winget"), \
                mock.patch.object(components, "_binary", return_value=None):
            optional = requirements.optional_missing()
            missing = requirements.report(["x"])
        self.assertTrue(all("winget install --id" in l for l in optional))
        self.assertIn(".\\scripts\\install.ps1", missing)

    def test_the_bridge_refuses_to_start_without_core(self):
        from agentaus_bridge.config import settings
        with mock.patch.object(settings, "agentaus_api_key", "k"), \
                mock.patch.object(requirements, "missing", return_value=["httpx - the bridge itself"]), \
                mock.patch("uvicorn.run") as run, \
                self.assertLogs("agentaus-bridge", level="ERROR") as caught:
            code = entry.main(["--env-file", "/nonexistent/.env"])
        self.assertEqual(code, 3)
        run.assert_not_called()
        self.assertTrue(any("httpx" in line for line in caught.output))

    def test_the_bridge_starts_with_chosen_components_missing_and_says_so(self):
        from agentaus_bridge.config import settings
        with mock.patch.object(settings, "agentaus_api_key", "k"), \
                mock.patch.object(requirements, "missing", return_value=[]), \
                mock.patch.object(requirements, "degraded", return_value=["OCR for scanned PDFs (tesseract)"]), \
                mock.patch.object(requirements, "skipped", return_value=["Office documents (LibreOffice)"]), \
                mock.patch("uvicorn.run") as run, \
                self.assertLogs("agentaus-bridge", level="INFO") as caught:
            entry.main(["--env-file", "/nonexistent/.env"])
        run.assert_called_once()
        self.assertTrue(any("WARNING" in line and "tesseract" in line for line in caught.output))
        self.assertTrue(any("INFO" in line and "skipped at install" in line and "LibreOffice" in line
                            for line in caught.output))


@unittest.skipIf(requirements.optional_missing(), "system document readers not installed")
class RealReaders(unittest.TestCase):
    """The installed tools themselves, not stubs: tested wherever they are installed.
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
        self.assertEqual(requirements.optional_missing(), [],
                         "run ./scripts/install.sh --preset full")

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

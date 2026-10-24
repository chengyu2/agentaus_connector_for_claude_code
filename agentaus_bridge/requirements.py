"""What the bridge needs installed before it serves a single request.

Every document reader is required, not optional. Without them the bridge used to run
regardless: a .docx or .xlsx was walked past as binary, a scanned PDF page came back
blank, and the run went on answering from what it could see - a tender response written
without the document that held its evidence looks like a weak answer, not a missing tool.
A machine that cannot read a document now refuses to start and says what to install.
"""

from __future__ import annotations

import importlib
import platform
import sys

MAC = platform.system() == "Darwin"

# (what, how it is found, why the bridge needs it, macOS install, Debian install)
SYSTEM = (
    ("LibreOffice", "soffice", "reads .docx, .xlsx, .pptx with their tables intact",
     "brew install --cask libreoffice", "sudo apt install libreoffice-writer libreoffice-calc libreoffice-impress"),
    ("poppler", "pdftotext", "the best PDF text tier, with column and table layout",
     "brew install poppler", "sudo apt install poppler-utils"),
    ("poppler", "pdftoppm", "renders scanned PDF pages for tesseract",
     "brew install poppler", "sudo apt install poppler-utils"),
    ("tesseract", "tesseract", "OCR for scanned PDF pages",
     "brew install tesseract", "sudo apt install tesseract-ocr"),
)
# (module, pip package, why)
PYTHON = (
    ("pdfminer.high_level", "pdfminer.six", "the pure-Python PDF tier"),
    ("pypdf", "pypdf", "the last PDF tier"),
    ("tree_sitter", "tree-sitter", "exact declarations in the search outline"),
    ("tree_sitter_language_pack", "tree-sitter-language-pack", "the outline's grammars"),
) + ((
    ("Vision", "pyobjc-framework-Vision", "macOS's own OCR, the first OCR tier"),
    ("Quartz", "pyobjc-framework-Quartz", "renders PDF pages for Vision"),
) if MAC else ())


def _module(name: str) -> bool:
    if name in sys.modules:
        return True
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001 - a broken install is as missing as an absent one
        return False


def missing() -> list[str]:
    """One line per missing requirement, each with the command that installs it."""
    from . import documents, pdf
    out = []
    for what, binary, why, mac, debian in SYSTEM:
        found = documents.soffice() if binary == "soffice" else pdf._tool(binary)
        if not found:
            out.append(f"{what} (`{binary}`) - {why}. Install: `{mac if MAC else debian}`")
    for module, package, why in PYTHON:
        if not _module(module):
            out.append(f"{package} - {why}. Install: `.venv/bin/pip install -r requirements.txt`")
    return out


def report(lines: list[str]) -> str:
    return ("The bridge needs every document reader installed and these are missing:\n"
            + "\n".join(f"  - {l}" for l in lines)
            + "\n\nOr run ./scripts/install.sh, which installs all of them.")

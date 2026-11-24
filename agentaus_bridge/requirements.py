"""What the bridge needs installed before it serves a single request.

The Python packages are required: install.sh puts them in the virtualenv, so they need
nothing from the machine. The system readers (LibreOffice, poppler, tesseract) are
optional. Without them a .docx or .xlsx is walked past and a scanned PDF page comes
back blank, so the bridge names each missing one at startup - a tender response written
without the document that held its evidence looks like a weak answer, not a missing tool.
"""

from __future__ import annotations

import importlib
import platform
import sys

MAC = platform.system() == "Darwin"
WIN = platform.system() == "Windows"

# (what, how it is found, why the bridge needs it, macOS install, Debian install, Windows install)
SYSTEM = (
    ("LibreOffice", "soffice", "reads .docx, .xlsx, .pptx with their tables intact",
     "brew install --cask libreoffice", "sudo apt install libreoffice-writer libreoffice-calc libreoffice-impress",
     "winget install --id TheDocumentFoundation.LibreOffice"),
    ("poppler", "pdftotext", "the best PDF text tier, with column and table layout",
     "brew install poppler", "sudo apt install poppler-utils",
     "winget install --id oschwartz10612.Poppler"),
    ("poppler", "pdftoppm", "renders scanned PDF pages for tesseract",
     "brew install poppler", "sudo apt install poppler-utils",
     "winget install --id oschwartz10612.Poppler"),
    ("tesseract", "tesseract", "OCR for scanned PDF pages",
     "brew install tesseract", "sudo apt install tesseract-ocr",
     "winget install --id UB-Mannheim.TesseractOCR"),
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
    """One line per missing required package, each with the command that installs it."""
    out = []
    for module, package, why in PYTHON:
        if not _module(module):
            out.append(f"{package} - {why}. Install: `.venv/bin/pip install -r requirements.txt`")
    return out


def optional_missing() -> list[str]:
    """One line per missing system reader, each with the command that installs it."""
    from . import documents, pdf
    out = []
    for what, binary, why, mac, debian, windows in SYSTEM:
        found = documents.soffice() if binary == "soffice" else pdf._tool(binary)
        if not found:
            cmd = windows if WIN else (mac if MAC else debian)
            out.append(f"{what} (`{binary}`) - {why}. Install: `{cmd}`")
    return out


def report(lines: list[str]) -> str:
    script = ".\\scripts\\install.ps1" if WIN else "./scripts/install.sh"
    return ("The bridge needs these Python packages and they are missing:\n"
            + "\n".join(f"  - {l}" for l in lines)
            + f"\n\nOr run {script}, which installs all of them.")


def optional_report(lines: list[str]) -> str:
    run = ("$env:INSTALL_DOCUMENT_READERS=1; .\\scripts\\install.ps1" if WIN
           else "INSTALL_DOCUMENT_READERS=1 ./scripts/install.sh")
    return ("Document reading is degraded; these readers are not installed:\n"
            + "\n".join(f"  - {l}" for l in lines)
            + f"\n\nOr run {run}.")

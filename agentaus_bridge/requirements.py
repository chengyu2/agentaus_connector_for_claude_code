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
import shutil
import sys

MAC = platform.system() == "Darwin"


def _family() -> str:
    """Which install commands to print: "mac", "fedora" (dnf) or "debian" (apt, and the
    fallback for any other Linux)."""
    if MAC:
        return "mac"
    try:
        release = platform.freedesktop_os_release()
    except (OSError, ValueError):  # absent, or not UTF-8
        release = {}
    ids = {release.get("ID", "")} | set(release.get("ID_LIKE", "").split())
    if ids & {"fedora", "rhel", "centos"}:
        return "fedora"
    if ids & {"debian", "ubuntu"}:
        return "debian"
    return "fedora" if shutil.which("dnf") and not shutil.which("apt-get") else "debian"


FAMILY = _family()

# (what, how it is found, why the bridge needs it, {family: install command})
SYSTEM = (
    ("LibreOffice", "soffice", "reads .docx, .xlsx, .pptx with their tables intact", {
        "mac": "brew install --cask libreoffice",
        "debian": "sudo apt install libreoffice-writer libreoffice-calc libreoffice-impress",
        "fedora": "sudo dnf install libreoffice-writer libreoffice-calc libreoffice-impress"}),
    ("poppler", "pdftotext", "the best PDF text tier, with column and table layout", {
        "mac": "brew install poppler",
        "debian": "sudo apt install poppler-utils",
        "fedora": "sudo dnf install poppler-utils"}),
    ("poppler", "pdftoppm", "renders scanned PDF pages for tesseract", {
        "mac": "brew install poppler",
        "debian": "sudo apt install poppler-utils",
        "fedora": "sudo dnf install poppler-utils"}),
    ("tesseract", "tesseract", "OCR for scanned PDF pages", {
        "mac": "brew install tesseract",
        "debian": "sudo apt install tesseract-ocr",
        "fedora": "sudo dnf install tesseract tesseract-langpack-eng"}),
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
    for what, binary, why, install in SYSTEM:
        found = documents.soffice() if binary == "soffice" else pdf._tool(binary)
        if not found:
            out.append(f"{what} (`{binary}`) - {why}. Install: `{install[FAMILY]}`")
    return out


def report(lines: list[str]) -> str:
    return ("The bridge needs these Python packages and they are missing:\n"
            + "\n".join(f"  - {l}" for l in lines)
            + "\n\nOr run ./scripts/install.sh, which installs all of them.")


def optional_report(lines: list[str]) -> str:
    return ("Document reading is degraded; these readers are not installed:\n"
            + "\n".join(f"  - {l}" for l in lines)
            + "\n\nOr run INSTALL_DOCUMENT_READERS=1 ./scripts/install.sh.")

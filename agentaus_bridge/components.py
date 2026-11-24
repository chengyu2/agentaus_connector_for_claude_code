"""The installable pieces of the bridge, and which of them this install chose.

Only `core` is required. Every other component switches a feature on: a document reader,
a PDF tier, the code outline, or a piece of setup (Claude Code settings, the background
service, the `agentaus-bridge` command). The installer records the choice in `.env` as
AGENTAUS_COMPONENTS, so the bridge can tell a reader the user skipped on purpose from one
that should be there and is not.

This module must import nothing beyond the standard library: the bridge reads it at
startup, and the installer reads it before the optional packages exist.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from dataclasses import dataclass, field

ENV_KEY = "AGENTAUS_COMPONENTS"


def platform_name() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


@dataclass(frozen=True)
class Component:
    key: str
    title: str
    loses: str                       # what goes missing without it
    size: str = ""
    kind: str = "python"             # python | system | setup
    pip: str | None = None           # requirements/<pip>.txt
    modules: tuple[str, ...] = ()    # python: every one must import
    binaries: tuple[str, ...] = ()   # system: every one must be found
    requires: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("darwin", "linux", "windows")
    # package manager -> package names. "brew-cask" is a separate manager for casks.
    packages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    locked: bool = False

    @property
    def runtime(self) -> bool:
        """Whether the running bridge uses it (setup components only matter at install)."""
        return self.kind in ("python", "system")


COMPONENTS: tuple[Component, ...] = (
    Component(
        "core", "Core bridge", "the bridge cannot run", "~60 MB", pip="core",
        modules=("fastapi", "uvicorn", "httpx", "tiktoken"), locked=True,
    ),
    Component(
        "pdf", "PDF text fallbacks (pdfminer, pypdf)",
        "PDFs are unreadable unless poppler is installed", "~10 MB", pip="pdf",
        modules=("pdfminer.high_level", "pypdf"),
    ),
    Component(
        "poppler", "PDF text with layout (poppler)",
        "PDF text loses its column and table layout", "~10 MB", kind="system",
        binaries=("pdftotext", "pdftoppm"),
        packages={"brew": ("poppler",), "apt": ("poppler-utils",), "dnf": ("poppler-utils",),
                  "pacman": ("poppler",), "winget": ("oschwartz10612.Poppler",)},
    ),
    Component(
        "ocr", "OCR for scanned PDFs (tesseract)",
        "scanned PDF pages come back blank", "~40 MB", kind="system",
        binaries=("tesseract",), requires=("poppler",),
        # Arch splits the language data out and asks which one unless it is named.
        packages={"brew": ("tesseract",), "apt": ("tesseract-ocr",), "dnf": ("tesseract",),
                  "pacman": ("tesseract", "tesseract-data-eng"),
                  "winget": ("UB-Mannheim.TesseractOCR",)},
    ),
    Component(
        "vision", "macOS Vision OCR",
        "OCR falls back to tesseract", "~5 MB", pip="vision",
        modules=("Vision", "Quartz"), platforms=("darwin",),
    ),
    Component(
        "office", "Office documents (LibreOffice)",
        ".docx, .xlsx and .pptx files are not read", "~500 MB", kind="system",
        binaries=("soffice",),
        packages={"brew-cask": ("libreoffice",),
                  "apt": ("libreoffice-writer", "libreoffice-calc", "libreoffice-impress"),
                  "dnf": ("libreoffice-writer", "libreoffice-calc", "libreoffice-impress"),
                  "pacman": ("libreoffice-fresh",),
                  "winget": ("TheDocumentFoundation.LibreOffice",)},
    ),
    Component(
        "outline", "Code outline (tree-sitter)",
        "the search outline falls back to a less precise one", "~50 MB", pip="outline",
        modules=("tree_sitter", "tree_sitter_language_pack"),
    ),
    Component(
        "settings", "Claude Code settings",
        "you add the env block to a settings.json by hand", kind="setup",
    ),
    Component(
        "service", "Background service",
        "you start the bridge yourself after every reboot", kind="setup",
    ),
    Component(
        "command", "agentaus-bridge command",
        "you manage the bridge with the scripts in scripts/", kind="setup",
    ),
)

BY_KEY = {c.key: c for c in COMPONENTS}

PRESETS: dict[str, tuple[str, ...]] = {
    "minimal": ("core", "settings"),
    "recommended": ("core", "pdf", "poppler", "vision", "outline", "settings", "service", "command"),
    "full": tuple(c.key for c in COMPONENTS),
}


def available(platform: str | None = None) -> list[Component]:
    platform = platform or platform_name()
    return [c for c in COMPONENTS if platform in c.platforms]


def resolve(keys, platform: str | None = None) -> list[str]:
    """The keys, plus core and everything they require, minus what this platform lacks,
    in COMPONENTS order. Unknown keys raise ValueError."""
    platform = platform or platform_name()
    wanted = {"core"}
    pending = list(keys)
    while pending:
        key = pending.pop()
        if key not in BY_KEY:
            raise ValueError(f"unknown component {key!r} (known: {', '.join(BY_KEY)})")
        if key not in wanted:
            wanted.add(key)
            pending.extend(BY_KEY[key].requires)
    return [c.key for c in COMPONENTS if c.key in wanted and platform in c.platforms]


def preset(name: str, platform: str | None = None) -> list[str]:
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r} (known: {', '.join(PRESETS)})")
    return resolve(PRESETS[name], platform)


def parse(value: str | None) -> list[str] | None:
    """AGENTAUS_COMPONENTS as a list of keys, or None when unset. Unknown keys are dropped,
    so an older bridge reading a newer .env still starts."""
    if value is None or not value.strip():
        return None
    return [k for k in (p.strip() for p in value.split(",")) if k in BY_KEY]


def selected() -> list[str]:
    """What this install chose. An install that predates components chose everything."""
    keys = parse(os.environ.get(ENV_KEY))
    if keys is None:
        return [c.key for c in available()]
    return resolve(keys)


# --- detection -------------------------------------------------------------------------

def _module(name: str) -> bool:
    if name in sys.modules:
        return True
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001 - a broken install is as missing as an absent one
        return False


def _binary(name: str) -> str | None:
    # The bridge's own lookups know where launchd-started processes and winget hide them.
    try:
        if name == "soffice":
            from . import documents
            return documents.soffice()
        from . import pdf
        return pdf._tool(name)
    except Exception:  # noqa: BLE001 - installer without the bridge's deps: plain PATH
        return shutil.which(name)


def present(component: Component) -> bool:
    if component.kind == "python":
        return all(_module(m) for m in component.modules)
    if component.kind == "system":
        return all(_binary(b) for b in component.binaries)
    return False


def package_manager(platform: str | None = None) -> str | None:
    platform = platform or platform_name()
    if platform == "darwin":
        return "brew" if shutil.which("brew") else None
    if platform == "windows":
        return "winget" if shutil.which("winget") else None
    for manager, exe in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman")):
        if shutil.which(exe):
            return manager
    return None


def install_hint(component: Component, platform: str | None = None) -> str:
    """One command that installs the component, for log lines and error messages."""
    platform = platform or platform_name()
    if component.kind == "python":
        pip = r".venv\Scripts\pip" if platform == "windows" else ".venv/bin/pip"
        return f"{pip} install -r requirements/{component.pip}.txt"
    manager = package_manager(platform)
    if manager == "brew":
        if "brew-cask" in component.packages:
            return "brew install --cask " + " ".join(component.packages["brew-cask"])
        return "brew install " + " ".join(component.packages.get("brew", ()))
    if manager == "winget":
        return "; ".join(f"winget install --id {p}" for p in component.packages.get("winget", ()))
    if manager in ("apt", "dnf"):
        return f"sudo {manager} install " + " ".join(component.packages.get(manager, ()))
    if manager == "pacman":
        return "sudo pacman -S --needed " + " ".join(component.packages.get(manager, ()))
    return f"install {', '.join(component.binaries)} with your package manager"

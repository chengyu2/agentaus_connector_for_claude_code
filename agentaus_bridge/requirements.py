"""What the bridge needs installed before it serves a single request.

Only the core packages are required. Everything else is a component (see components.py):
a document reader, a PDF tier, the code outline. The installer records which ones the user
chose in AGENTAUS_COMPONENTS, and startup tells the two cases apart:

- a chosen component that is missing is a broken install, so it is a warning that names
  the command that fixes it - a tender response written without the document that held
  its evidence looks like a weak answer, not a missing tool;
- a component the user skipped is a choice, so it is one info line.

An install that predates AGENTAUS_COMPONENTS counts as having chosen everything.
"""

from __future__ import annotations

import platform

from . import components

MAC = platform.system() == "Darwin"
WIN = platform.system() == "Windows"


def _module(name: str) -> bool:
    return components._module(name)


def _platform() -> str:
    return "windows" if WIN else ("darwin" if MAC else "linux")


def _script() -> str:
    return ".\\scripts\\install.ps1" if WIN else "./scripts/install.sh"


def missing() -> list[str]:
    """One line per missing core package. Without these the bridge cannot start."""
    core = components.BY_KEY["core"]
    hint = components.install_hint(core, _platform())
    return [f"{m} - the bridge itself. Install: `{hint}`"
            for m in core.modules if not _module(m)]


def report(lines: list[str]) -> str:
    return ("The bridge needs these Python packages and they are missing:\n"
            + "\n".join(f"  - {l}" for l in lines)
            + f"\n\nOr run {_script()}, which installs them.")


def _line(component: components.Component) -> str:
    return (f"{component.title} - without it {component.loses}. "
            f"Install: `{components.install_hint(component, _platform())}`")


def degraded(chosen: list[str] | None = None) -> list[str]:
    """One line per chosen runtime component that is not actually installed."""
    chosen = components.selected() if chosen is None else chosen
    return [_line(c) for c in components.available(_platform())
            if c.runtime and not c.locked and c.key in chosen and not components.present(c)]


def degraded_report(lines: list[str]) -> str:
    return ("These components were chosen at install but are not working:\n"
            + "\n".join(f"  - {l}" for l in lines)
            + f"\n\nOr re-run {_script()} (or `agentaus-bridge components`) to repair them.")


def skipped(chosen: list[str] | None = None) -> list[str]:
    """The titles of runtime components this install chose not to have."""
    chosen = components.selected() if chosen is None else chosen
    return [c.title for c in components.available(_platform())
            if c.runtime and c.key not in chosen]


def optional_missing() -> list[str]:
    """One line per system reader that is not installed, whatever was chosen."""
    return [_line(c) for c in components.available(_platform())
            if c.kind == "system" and not components.present(c)]

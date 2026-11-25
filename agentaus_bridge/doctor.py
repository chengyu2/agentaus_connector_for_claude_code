"""`python -m agentaus_bridge --doctor`: one-line checks of the install and config.

Each check returns ``(ok: bool, text: str)``. ``run()`` prints one line per check with
a checkmark or a cross (ASCII fallback when the console cannot print the glyphs) and
returns the process exit code: 0 when everything passes, 1 when any check failed.

Document-reader and dependency install commands are chosen per OS so the
fixes match it (PowerShell/winget on Windows, brew on macOS, apt on Linux).

The live credential check (6) needs the network and is skipped with ``--offline``.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

from . import requirements
from .config import settings

# The pip package -> import module pairs the bridge needs, for check 3. The same
# mapping lives in requirements.py; kept inline here so this check is independent
# of whether the document readers are installed.
DEPS = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("httpx", "httpx"),
    ("tiktoken", "tiktoken"),
    ("tree-sitter", "tree_sitter"),
    ("tree-sitter-language-pack", "tree_sitter_language_pack"),
    ("pdfminer.six", "pdfminer.high_level"),
    ("pypdf", "pypdf"),
)

WINDOWS = requirements.WIN
MAC = requirements.MAC

# winget package ids, used on Windows instead of the apt/brew commands.
WINGET = {
    "soffice": "winget install --id TheDocumentFoundation.LibreOffice -e",
    "pdftotext": "winget install --id oschwartz10612.Poppler -e",
    "pdftoppm": "winget install --id oschwartz10612.Poppler -e",
    "tesseract": "winget install --id UB-Mannheim.TesseractOCR -e",
}

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


def _venv_python() -> str:
    return r".\.venv\Scripts\python.exe" if WINDOWS else ".venv/bin/python"


def _pip() -> str:
    return r".venv\Scripts\pip" if WINDOWS else ".venv/bin/pip"


def _emit(ok: bool, text: str) -> None:
    mark = "✅" if ok else "❌"
    try:
        print(f"{mark} {text}")
    except UnicodeEncodeError:
        print(f"{'[OK]' if ok else '[X]'} {text}")


# --- individual checks -----------------------------------------------------

def check_python_version() -> tuple[bool, str]:
    import platform
    ok = sys.version_info >= (3, 9)
    return ok, f"Python {platform.python_version()} (needs 3.9+) found"

def check_venv() -> tuple[bool, str]:
    running = str(Path(sys.executable)).lower()
    if ".venv" in running:
        return True, f"running under the repo venv ({sys.executable})"
    repo_venv = Path.cwd() / ".venv"
    if repo_venv.is_dir():
        return False, (f"not running the repo venv. Run: {_venv_python()} -m agentaus_bridge --doctor")
    return False, (f"no .venv at {repo_venv}. Create it, then run: {_venv_python()} -m agentaus_bridge --doctor")

def check_dependencies() -> tuple[bool, str]:
    missing = [label for label, module in DEPS if not requirements._module(module)]
    if missing:
        return False, f"missing Python package(s): {', '.join(missing)}. Install: {_pip()} install -r requirements.txt"
    return True, f"all {len(DEPS)} Python dependencies import"

def check_readers() -> tuple[bool, str]:
    missing_lines = []
    for what, binary, why, mac, debian in SYSTEM:
        if binary == "soffice":
            from . import documents
            found = bool(documents.soffice())
        else:
            from . import pdf
            found = bool(pdf._tool(binary))
        if not found:
            how = WINGET[binary] if WINDOWS else (mac if MAC else debian)
            missing_lines.append(f"{what} (`{binary}`) missing - {why}. Install: `{how}`")
    if missing_lines:
        return False, "; ".join(missing_lines)
    return True, "document readers found: soffice, pdftotext, pdftoppm, tesseract"

def check_env_key() -> tuple[bool, str]:
    env = Path.cwd() / ".env"
    if not env.is_file():
        return False, ".env missing - copy .env.example to .env and add AGENTAUS_API_KEY"
    if not settings.agentaus_api_key:
        return False, ".env exists but AGENTAUS_API_KEY is not set - add it to .env"
    return True, ".env exists and AGENTAUS_API_KEY is set"

def check_live() -> tuple[bool, str]:
    if not settings.agentaus_api_key:
        return False, "AGENTAUS_API_KEY is not set - set it in .env (see check above)"
    import httpx
    try:
        response = httpx.post(
            settings.agentaus_url,
            headers={"Authorization": f"Bearer {settings.agentaus_api_key}",
                     "Content-Type": "application/json"},
            json={"messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                 "stream": False},
            timeout=30,
        )
    except httpx.HTTPError as exc:
        return False, f"connection failed ({exc}) - check the network / AGENTAUS_BASE_URL"
    if response.status_code != 200:
        return False, f"Agentaus answered HTTP {response.status_code} - is AGENTAUS_API_KEY or the endpoint wrong?"
    return True, "one live call to Agentaus answered OK"

def check_port() -> tuple[bool, str]:
    host = "127.0.0.1" if settings.host == "localhost" else settings.host
    import socket as _sock
    probe = _sock.socket()
    try:
        probe.bind((host, settings.port))
    except OSError:
        pass
    else:
        probe.close()
        return True, f"port {settings.port} is free"
    host_str = "127.0.0.1" if host == "::1" else host
    import httpx
    try:
        health = httpx.get(f"http://{host_str}:{settings.port}/healthz", timeout=3)
        if health.status_code == 200:
            return True, f"port {settings.port} is in use by a running bridge (/healthz says OK)"
        return False, (f"port {settings.port} is in use by something that is not this bridge "
                       f"(HTTP {health.status_code}, not /healthz)")
    except Exception:
        return False, f"port {settings.port} is in use but /healthz did not answer - something else holds it. Stop it, or set BRIDGE_PORT in .env"

def check_host_token() -> tuple[bool, str]:
    loopback = settings.host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not settings.bridge_token:
        return False, (f"BRIDGE_HOST={settings.host} exposes the bridge to your whole network "
                       "with no BRIDGE_TOKEN - anyone on the Wi-Fi could spend your Agentaus "
                       "credit. Set BRIDGE_TOKEN in .env")
    return True, f"BRIDGE_HOST={settings.host}, BRIDGE_TOKEN {'set' if settings.bridge_token else 'empty (fine on loopback)'}"

def check_claude_settings() -> tuple[bool, str]:
    paths = [Path.home() / ".claude" / "settings.json", Path.cwd() / ".claude" / "settings.json"]
    expected = f"http://127.0.0.1:{settings.port}" if settings.host == "localhost" else f"http://{settings.host}:{settings.port}"
    found_any = False
    problems = []
    for path in paths:
        if not path.is_file():
            continue
        found_any = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path}: invalid JSON ({exc})")
            continue
        env = data.get("env") or {}
        base = str(env.get("ANTHROPIC_BASE_URL") or "")
        if expected not in base:
            problems.append(f"{path}: ANTHROPIC_BASE_URL ({base!r}) does not point at this bridge ({expected})")
        if "agentaus" not in str(env.get("ANTHROPIC_CUSTOM_MODEL_OPTION") or "").lower():
            problems.append(f"{path}: ANTHROPIC_CUSTOM_MODEL_OPTION does not contain 'agentaus' "
                            f"({env.get('ANTHROPIC_CUSTOM_MODEL_OPTION')!r})")
    if not found_any:
        return False, ("no Claude Code settings found - check ~/.claude/settings.json and .claude/settings.json "
                       "have env.ANTHROPIC_BASE_URL and env.ANTHROPIC_CUSTOM_MODEL_OPTION for this bridge")
    if problems:
        return False, "; ".join(problems)
    return True, f"Claude Code settings point at {expected} with model option 'agentaus'"

def check_skills() -> tuple[bool, str]:
    folder = Path.cwd() / ".claude" / "skills"
    tip = "VS Code must have this repo folder open (File > Open Folder) for /courtroom and other skills to appear."
    if folder.is_dir():
        return True, f".claude/skills exists ({tip})"
    return False, f".claude/skills missing at {folder}. Open this repo folder in VS Code ({tip})"

def check_gitignore() -> tuple[bool, str]:
    gi = Path.cwd() / ".gitignore"
    text = gi.read_text(encoding="utf-8", errors="replace") if gi.is_file() else ""
    missing = [pat for pat in (".env", "*.log", ".venv/") if pat not in text]
    if missing:
        return False, f".gitignore missing pattern(s): {', '.join(missing)}"
    too_big = []
    try:
        proc = subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, timeout=15)
        if proc.returncode == 0:
            for name in (proc.stdout or "").split("\0"):
                if not name:
                    continue
                p = Path.cwd() / name
                try:
                    if p.is_file() and p.stat().st_size > 50 * 1024 * 1024:
                        too_big.append(str(p))
                except OSError:
                    pass
    except Exception:
        pass
    if too_big:
        return False, f"tracked file(s) over 50 MB: {', '.join(too_big)} - add them to .gitignore"
    return True, ".gitignore covers .env, *.log, .venv/; no tracked file over 50 MB"

CHECKS_WITHOUT_NETWORK = (
    ("Python version 3.9+", check_python_version),
    ("Running in the repo venv", check_venv),
    ("Python dependencies", check_dependencies),
    ("Document readers", check_readers),
    (".env and AGENTAUS_API_KEY", check_env_key),
    ("Port", check_port),
    ("Host / token", check_host_token),
    ("Claude Code settings", check_claude_settings),
    ("Skills folder", check_skills),
    (".gitignore", check_gitignore),
)

CHECKS = CHECKS_WITHOUT_NETWORK + (("API credential (live)", check_live),)


def run(offline: bool = False) -> int:
    """Run the checks, print one line each, return 0 (all pass) or 1 (any fail)."""
    checks = CHECKS_WITHOUT_NETWORK if offline else CHECKS
    failed = 0
    for label, fn in checks:
        try:
            ok, text = fn()
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check
            ok, text = False, f"check errored: {exc}"
        _emit(ok, text)
        if not ok:
            failed += 1
    if offline:
        print("(offline: live API credential check skipped)")
    return 1 if failed else 0

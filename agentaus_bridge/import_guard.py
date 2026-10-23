"""Whether a graph main loop's edit to a .py file leaves the module importable.

Measured (d13 c1913h): after the graph, the main loop re-added the request's `--json` option
and then de-indented it out of `if __name__ == '__main__':` - `args = parser.parse_args()` at
module level. Bash printed nothing after the edit, nothing checked it, and every tool that
imported the module died with "unrecognized arguments": the run scored 0 where the file the
graph left graded 0.167. The graph checks imports in its own Assemble phase; nothing checked
the main loop's edits after it.

So the bridge snapshots each .py file a graph main loop is about to Edit or Write, and when the
call's result comes back it imports the file twice on a temporary copy of the project - as it
was, and as it is now - with argv that no command line accepts. Only a file that imported
cleanly before and fails now is reported, so a module that never imported (a package-relative
script, a missing dependency) is never flagged. The finding goes into that call's tool result.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict

log = logging.getLogger("agentaus-bridge")

_WRITES = ("Edit", "MultiEdit", "Write")
_PRISTINE: "OrderedDict[str, tuple[str, str, str]]" = OrderedDict()   # call id -> (path, root, text)
_LIMIT = 256
_COPY_FILES = 3000
_COPY_BYTES = 50 * 1024 * 1024
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache",
              ".mypy_cache", ".tox"}
IMPORT_SECONDS = 8.0

# Runs in a subprocess, in the temporary copy. argv is one no parser accepts; stdin is empty.
_PROBE = r"""
import contextlib, importlib, importlib.util, io, os, sys
sys.dont_write_bytecode = True
folder, module, path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.argv = [path, "--agentaus-import-check"]
for p in (os.getcwd(), folder):
    if p not in sys.path:
        sys.path.insert(0, p)
out = io.StringIO()
try:
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        if module:
            importlib.import_module(module)
        else:
            spec = importlib.util.spec_from_file_location("agentaus_import_check", path)
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
    print("IMPORT OK")
except BaseException as exc:
    detail = (out.getvalue().strip().splitlines() or [""])[-1] or str(exc)
    print("IMPORT FAILED", type(exc).__name__ + ":", detail[-300:])
"""


def forget() -> None:
    """Drop every snapshot. For tests: it is module state."""
    _PRISTINE.clear()


def _args(call: dict) -> dict:
    raw = call.get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def remember(calls: list, cwd: str | None) -> int:
    """Snapshot each existing .py file these Edit/MultiEdit/Write calls are about to change,
    by call id. Call it before the calls reach the client."""
    if not cwd or not os.path.isdir(cwd):
        return 0
    root = os.path.normpath(cwd)
    kept = 0
    for call in calls:
        if call.get("name") not in _WRITES or not call.get("id"):
            continue
        path = _args(call).get("file_path")
        if not isinstance(path, str) or not path.endswith(".py") or not os.path.isabs(path):
            continue
        path = os.path.normpath(path)
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        # Two calls on one file in one response: both keep the text from before either.
        earlier = next((t for p, _, t in _PRISTINE.values() if p == path), None)
        _PRISTINE[call["id"]] = (path, root, earlier if earlier is not None else text)
        _PRISTINE.move_to_end(call["id"])
        kept += 1
    while len(_PRISTINE) > _LIMIT:
        _PRISTINE.popitem(last=False)
    return kept


def _module_name(path: str, root: str) -> tuple[str, str]:
    """(directory to put on sys.path, dotted module name) - a package module is imported as
    one, so its relative imports work; a loose file by path ("" name)."""
    folder, name = os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]
    parts = [name]
    while os.path.isfile(os.path.join(folder, "__init__.py")) and folder.startswith(root):
        parts.insert(0, os.path.basename(folder))
        folder = os.path.dirname(folder)
    if len(parts) == 1:
        return folder, ""
    return folder, ".".join(p for p in parts if p != "__init__")


def _copy_project(root: str, dest: str) -> bool:
    count = size = 0
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith(".pyc"):
                continue
            src = os.path.join(here, name)
            try:
                size += os.path.getsize(src)
            except OSError:
                continue
            count += 1
            if count > _COPY_FILES or size > _COPY_BYTES:
                return False
            target = os.path.join(dest, os.path.relpath(src, root))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                shutil.copy2(src, target)
            except OSError:
                pass
    return True


def _python() -> str:
    return shutil.which("python3") or sys.executable


def probe(root: str, path: str, text: str, copy: str) -> tuple[bool, str]:
    """(imports cleanly, what failed) for `text` as the file at `path`, in the copy."""
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc.msg} (line {exc.lineno})"
    target = os.path.join(copy, os.path.relpath(path, root))
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    folder, module = _module_name(target, copy)
    try:
        done = subprocess.run([_python(), "-c", _PROBE, folder, module, target], cwd=copy,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=IMPORT_SECONDS,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return False, f"importing it did not finish in {IMPORT_SECONDS:.0f}s"
    except OSError as exc:
        return True, f"not checked ({exc})"
    line = (done.stdout.strip().splitlines() or [""])[-1]
    if line.startswith("IMPORT OK"):
        return True, ""
    return False, line[len("IMPORT FAILED"):].strip() or (done.stderr.strip()[-300:] or "it failed")


def finding(path: str, root: str, before: str) -> str | None:
    """The correction for an edit that broke importing `path`, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            now = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    if now == before:
        return None
    with tempfile.TemporaryDirectory(prefix="agentaus-import-") as copy:
        if not _copy_project(root, copy):
            return None
        ok_before, _ = probe(root, path, before, copy)
        if not ok_before:
            return None
        ok_now, why = probe(root, path, now, copy)
    if ok_now:
        return None
    log.warning("import guard: %s imported before the edit and fails now (%s)", path, why)
    return (f"[bridge] Import check: `{path}` imported cleanly before this edit and now fails "
            f"when imported - {why}. Importing a module must not run command-line code: keep "
            "argument parsing and other script code under `if __name__ == '__main__':`, then "
            "run the request's commands again.")


def check(body: dict) -> tuple[dict, int]:
    """`body` with a correction added to the newest tool result of each remembered edit that
    broke its module's import. Each snapshot is used once."""
    messages = list(body.get("messages") or [])
    if not messages or messages[-1].get("role") != "user" or not _PRISTINE:
        return body, 0
    content = messages[-1].get("content")
    if not isinstance(content, list):
        return body, 0
    by_path: dict = {}
    for index, block in enumerate(content):
        if (isinstance(block, dict) and block.get("type") == "tool_result"
                and block.get("tool_use_id") in _PRISTINE):
            path, root, before = _PRISTINE.pop(block["tool_use_id"])
            if not block.get("is_error"):
                first = by_path.setdefault(path, [root, before, index])
                first[2] = index
    blocks, added = list(content), 0
    for path, (root, before, index) in by_path.items():
        note = finding(path, root, before)
        if not note:
            continue
        block = blocks[index]
        inner = block.get("content")
        inner = [{"type": "text", "text": inner}] if isinstance(inner, str) else list(inner or [])
        blocks[index] = {**block, "content": inner + [{"type": "text", "text": note}]}
        added += 1
    if not added:
        return body, 0
    messages[-1] = {**messages[-1], "content": blocks}
    return {**body, "messages": messages}, added

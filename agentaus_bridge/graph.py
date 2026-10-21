"""The agent graph the bridge runs when Agentaus calls `Workflow`.

Measured over eight ultracode rounds, the weakest thing Agentaus did was write the
workflow: scripts on one line, unbalanced brackets, `Bash()` called inside a script,
subagent prompts like "compute the required statistics" with no rules in them, a
verifier told to "repeat the calculations as before", and no result ever passed from one
agent to the next. Every fix aimed at the script it wrote was a rule it then had to follow.

So the bridge supplies the graph instead (`graphs/solve.js`), and Agentaus only decides to
run it. It is built on four principles, each from how this model behaves: many agents;
one small, simple prompt per agent; many steps; and redundancy - a backup for every step
that can fail, and every answer reached three independent ways before it is trusted.

The substitution happens on the model's tool call, before Claude Code sees it: the
script is replaced, and `args` carries the user's request and the working directory. A
call naming a saved workflow, resuming a run, or re-running a script file is the user's
or Claude Code's own and passes through untouched.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from xml.etree import ElementTree

from .config import settings

log = logging.getLogger("agentaus-bridge")

MARK = "[agentaus-graph]"
_GRAPHS = Path(__file__).parent / "graphs"
SCRIPT = (_GRAPHS / "solve.js").read_text()
DYNAMIC_SCRIPT = (_GRAPHS / "dynamic.js").read_text()
# Every role in the dynamic graph works from a skill file: scout, plan, review-plan, solve,
# merge, check, assemble, fix. Passed in with the script, so no agent spends a call reading
# its own instructions off disk.
SKILLS = {p.stem: p.read_text() for p in sorted((_GRAPHS / "skills").glob("*.md"))}


def script_for() -> str:
    return DYNAMIC_SCRIPT if settings.agentaus_graph_kind == "dynamic" else SCRIPT


def expand(call: dict, task: str, cwd: str | None) -> dict:
    """`call` with the graph in place of the script Agentaus wrote, or unchanged."""
    if call.get("name") != "Workflow":
        return call
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    if any(arguments.get(k) for k in ("name", "resumeFromRunId", "scriptPath")):
        return call
    dynamic = settings.agentaus_graph_kind == "dynamic"
    expanded = {
        "description": arguments.get("description") or "Agentaus agent graph",
        "script": script_for(),
        "args": {"task": task, "cwd": cwd or "", "width": settings.agentaus_graph_width,
                 **({"skills": SKILLS, **prepare(cwd)} if dynamic else {})},
    }
    log.info("workflow: running the agent graph for a %d-char request", len(task or ""))
    return {**call, "arguments": json.dumps(expanded)}


# --- What the bridge prepares before the dynamic graph starts ------------------------------
#
# Each of these was a job an agent did by hand, badly. Measured on the last round: the scout
# typed plain-text copies of .docx files itself - truncated, with lines it invented - and a
# pricing table was missing from the copy every later agent read; solvers copied the project
# after others had already edited it; and planners guessed function names. A script does each
# exactly, once, before any agent runs.

def scratch_of(cwd: str | None) -> str:
    """The graph's scratch folder - the same expression as SCRATCH in graphs/dynamic.js."""
    name = re.sub(r"[^\w.-]+", "_", re.sub(r"^/+", "", cwd or "") or "task", flags=re.A)
    return f"/tmp/agentaus-graph/{name}-dyn"


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache",
              ".mypy_cache"}
_SNAPSHOT_BYTES = 200 * 1024 * 1024
_SNAPSHOT_FILES = 20_000
_MAX_DOCUMENTS = 30
_MAX_CODE_FILES = 40
_MAX_FUNCTIONS = 300
_DOC_CHARS = 2_000


def _project_files(cwd: str) -> list[str]:
    """Every file under cwd the graph cares about, relative, in a stable order."""
    out = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            if not name.endswith(".pyc"):
                out.append(os.path.relpath(os.path.join(root, name), cwd))
    return out


def _snapshot(cwd: str, files: list[str], dest: str) -> str:
    """A copy of the project as it was before any agent touched it, or "" if too big."""
    size = 0
    for rel in files:
        try:
            size += os.lstat(os.path.join(cwd, rel)).st_size
        except OSError:
            pass
    if len(files) > _SNAPSHOT_FILES or size > _SNAPSHOT_BYTES:
        log.info("graph prep: no pristine snapshot - %d files, %d MB", len(files), size >> 20)
        return ""
    shutil.copytree(cwd, dest, symlinks=True, ignore_dangling_symlinks=True,
                    ignore=shutil.ignore_patterns(*_SKIP_DIRS, "*.pyc"))
    return dest


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _w_paragraph(p) -> str:
    parts = []
    for node in p.iter():
        if node.tag == _W + "t":
            parts.append(node.text or "")
        elif node.tag == _W + "tab":
            parts.append("\t")
        elif node.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _w_blocks(parent, lines: list) -> None:
    """Body content in document order: a line per paragraph, a line per table row."""
    for child in parent:
        if child.tag == _W + "p":
            lines.append(_w_paragraph(child))
        elif child.tag == _W + "tbl":
            for row in child.iter(_W + "tr"):
                cells = [" ".join(t for t in (_w_paragraph(p).strip() for p in cell.iter(_W + "p")) if t)
                         for cell in row if cell.tag == _W + "tc"]
                lines.append(" | ".join(cells))
        elif child.tag in (_W + "sdt", _W + "sdtContent", _W + "customXml", _W + "ins",
                           _W + "smartTag"):
            # Content controls and the like wrap ordinary paragraphs and tables.
            _w_blocks(child, lines)


def docx_text(path: str) -> str:
    """A .docx as plain text, tables included, with nothing but the standard library."""
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    body = root.find(_W + "body")
    lines: list = []
    _w_blocks(body if body is not None else root, lines)
    return "\n".join(lines)


def pptx_text(path: str) -> str:
    """A .pptx as plain text: each slide's paragraphs, in slide order."""
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        out = []
        for k, name in enumerate(slides, 1):
            out.append(f"--- slide {k} ---")
            root = ElementTree.fromstring(z.read(name))
            for p in root.iter(_A + "p"):
                text = "".join(t.text or "" for t in p.iter(_A + "t"))
                if text.strip():
                    out.append(text)
        return "\n".join(out)


def _document_text(path: str) -> str:
    from . import documents
    lower = path.lower()
    if lower.endswith(".pdf"):
        # pdf.extract, through the documents cache: a second run on the same project is free.
        return documents.extract(path)
    if documents.available(path):
        # LibreOffice keeps more of the layout, when it is there.
        text = documents.extract(path)
        if text.strip():
            return text
    return docx_text(path) if lower.endswith(".docx") else pptx_text(path)


def _texts(cwd: str, files: list[str], inputs: str) -> list:
    out = []
    for rel in [f for f in files if f.lower().endswith((".docx", ".pdf", ".pptx"))][:_MAX_DOCUMENTS]:
        try:
            text = _document_text(os.path.join(cwd, rel))
        except Exception as exc:  # noqa: BLE001 - one unreadable file is not the run's problem
            log.warning("graph prep: could not read %s (%s)", rel, exc)
            continue
        if not text.strip():
            continue
        dest = os.path.join(inputs, rel + ".txt")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        out.append({"source": rel, "text": dest, "lines": text.count("\n") + 1})
    return out


def _is_stub(fn) -> bool:
    for k, node in enumerate(fn.body):
        if k == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.Pass) or (isinstance(node, ast.Expr) and isinstance(
                node.value, ast.Constant) and node.value.value is Ellipsis):
            continue
        if isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                continue
        return False
    return True


# A module-level name is a table or a threshold when its value is data: a literal, a
# container, a comprehension. `d = Doc(...)` and `BANK = load()` are not.
_DATA = (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.DictComp, ast.ListComp,
         ast.SetComp, ast.BinOp, ast.UnaryOp, ast.JoinedStr)
_CONTAINERS = {"dict", "list", "set", "frozenset", "tuple", "OrderedDict", "defaultdict"}


def _is_data(value) -> bool:
    if isinstance(value, ast.Call):
        func = value.func
        # `"Achiever Activator ...".split()` is a table too.
        if isinstance(func, ast.Attribute) and _is_data(func.value):
            return True
        return (func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")) in _CONTAINERS
    return isinstance(value, _DATA)


def _inventory(cwd: str, files: list[str]) -> tuple[list, list]:
    functions, constants = [], []
    code = [f for f in files if f.endswith(".py") and not os.path.basename(f).startswith("test")]
    for rel in code[:_MAX_CODE_FILES]:
        try:
            with open(os.path.join(cwd, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
        except (OSError, SyntaxError, ValueError) as exc:
            log.info("graph prep: could not parse %s (%s)", rel, exc)
            continue
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if len(functions) >= _MAX_FUNCTIONS:
                    continue
                returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                functions.append({
                    "file": rel, "fn": node.name, "line": node.lineno,
                    "signature": f"def {node.name}({ast.unparse(node.args)}){returns}",
                    "doc": (ast.get_docstring(node) or "")[:_DOC_CHARS], "stub": _is_stub(node)})
            elif isinstance(node, ast.Assign) and _is_data(node.value):
                names += [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.value is not None and _is_data(node.value):
                names.append(node.target.id)
        names = [n for n in dict.fromkeys(names) if not (n.startswith("__") and n.endswith("__"))]
        if names:
            constants.append({"file": rel, "names": names})
    return functions, constants


def _make_writable(path: str) -> None:
    # A project copied with read-only files would otherwise survive rmtree on macOS.
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), 0o755, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass


def prepare(cwd: str | None) -> dict:
    """The args the bridge adds for the dynamic graph: pristine, texts, functions, constants,
    examples. Best effort - a step that fails is logged and left out, never raised."""
    extra: dict = {"pristine": "", "texts": [], "functions": [], "constants": [], "examples": ""}
    if not cwd or not os.path.isdir(cwd):
        return extra
    scratch = scratch_of(cwd)
    started = time.monotonic()
    try:
        if os.path.exists(scratch):
            _make_writable(scratch)
        shutil.rmtree(scratch, ignore_errors=True)
        os.makedirs(scratch, exist_ok=True)
        files = _project_files(cwd)
    except Exception as exc:  # noqa: BLE001
        log.warning("graph prep: could not set up %s (%s)", scratch, exc)
        return extra
    steps = (
        ("pristine", lambda: _snapshot(cwd, files, os.path.join(scratch, "pristine"))),
        ("texts", lambda: _texts(cwd, files, os.path.join(scratch, "inputs"))),
        ("inventory", lambda: _inventory(cwd, files)),
        ("examples", lambda: _write(os.path.join(scratch, "examples.py"), EXAMPLES_SCRIPT)),
    )
    for name, step in steps:
        try:
            got = step()
        except Exception as exc:  # noqa: BLE001
            log.warning("graph prep: %s failed (%s)", name, exc)
            continue
        if name == "inventory":
            extra["functions"], extra["constants"] = got
        else:
            extra[name] = got
    log.info("graph prep: %s in %.1fs - snapshot %s, %d document(s), %d function(s), "
             "%d file(s) of constants", scratch, time.monotonic() - started,
             "yes" if extra["pristine"] else "no", len(extra["texts"]),
             len(extra["functions"]), len(extra["constants"]))
    return extra


def _write(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# Docstring examples as tests. Measured: solvers implemented ten HumanEval functions from the
# prose and never ran the `>>>` lines two inches above it; half the misses were examples the
# docstring itself gave. Run against the ORIGINAL file's docstrings, so a candidate cannot
# pass by editing the examples.
EXAMPLES_SCRIPT = r'''"""Docstring examples as tests. usage: examples.py SPEC.py CANDIDATE.py [function]
Reads examples from SPEC (the original, untouched file) and runs them on CANDIDATE."""
import ast, os, re, signal, sys
spec, cand = sys.argv[1], sys.argv[2]
only = sys.argv[3] if len(sys.argv) > 3 else None
ARROW = re.compile(r'^\s*(?:>>>\s*)?(?P<call>[A-Za-z_]\w*\(.*\))\s*(?:#\s*)?(?:==>|=>|==|➞|->|returns|should return)\s*(?P<want>.+?)\s*$')
def examples(fn):
    doc = ast.get_docstring(fn) or ''
    lines = doc.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        m = ARROW.match(s)
        if m and m.group('call').startswith(fn.name + '('):
            yield m.group('call'), m.group('want').rstrip('.').strip(); continue
        if s.startswith('>>> ' + fn.name + '(') and i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].strip().startswith('>>>'):
            yield s[4:], lines[i + 1].strip()
def timeout(*_):
    raise TimeoutError('took over 10s')
if hasattr(signal, 'SIGALRM'):
    signal.signal(signal.SIGALRM, timeout)
def limited(thunk):
    if hasattr(signal, 'SIGALRM'):
        signal.alarm(10)
    try:
        return thunk()
    finally:
        if hasattr(signal, 'SIGALRM'):
            signal.alarm(0)
cases = [(call, want) for fn in ast.parse(open(spec).read()).body
         if isinstance(fn, ast.FunctionDef) and not (only and fn.name != only)
         for call, want in examples(fn)]
# Not '__main__': a candidate's `if __name__ == '__main__':` block must not run (or wait on stdin).
ns = {'__name__': 'candidate', '__file__': os.path.abspath(cand)}
sys.path.insert(0, os.path.dirname(os.path.abspath(cand)))
sys.argv = [cand]
try:
    limited(lambda: exec(compile(open(cand).read(), cand, 'exec'), ns))
except BaseException as e:
    print(f'MISMATCH import {cand}: {e!r}')
    print(f'0/{len(cases)} docstring examples pass')
    sys.exit(0)
bad = n = 0
for call, want in cases:
    n += 1
    want = want.split(' = ')[-1]
    # "'No' (the name should start with a letter)": the longest leading part that is a value.
    words, exp = want.split(' '), KeyError
    for k in range(len(words), 0, -1):
        try:
            exp, want = eval(' '.join(words[:k]), {}), ' '.join(words[:k])
            break
        except Exception:
            pass
    if exp is KeyError:
        print(f'skip     {call} (cannot read expected value {want!r})'); n -= 1; continue
    try:
        got = limited(lambda: eval(call, ns))
        ok = got == exp
    except BaseException as e:
        got, ok = f'error {e!r}', False
    bad += not ok
    print(('ok       ' if ok else 'MISMATCH ') + f'{call} -> {got!r}' + ('' if ok else f'   docstring says {want}'))
print(f'{n - bad}/{n} docstring examples pass')
'''


def script_of(call: dict) -> str:
    """The inline script a Workflow call carries, or ""."""
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return ""
    return (arguments or {}).get("script") or "" if isinstance(arguments, dict) else ""


def is_graph_task(body: dict) -> bool:
    """Whether a subagent request was started by the graph - its prompt carries MARK."""
    for message in body.get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or [] if isinstance(b, dict)]
        if any(MARK in (t or "") for t in texts):
            return True
    return False


# A graph subagent has one small job. These are all it needs; the rest - starting agents
# or workflows of its own, skills, the web, MCP connectors - is room to wander off.
GRAPH_TOOLS = {"Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS",
               "StructuredOutput"}


THINK = "[think-only]"


def _is_think_only(body: dict) -> bool:
    for message in body.get("messages") or []:
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or [] if isinstance(b, dict)]
        if any(THINK in (t or "") and MARK in (t or "") for t in texts):
            return True
    return False


def focus(body: dict) -> tuple[dict, list]:
    """A graph subagent's request with only the tools its job uses.

    A think-only agent - one the graph marks as reasoning over text it was handed - gets
    `StructuredOutput` alone, so it answers in one call instead of a tool loop.
    """
    tools = body.get("tools") or []
    think = _is_think_only(body)
    allowed = {"StructuredOutput"} if think else GRAPH_TOOLS
    kept = [t for t in tools if (t or {}).get("name") in allowed]
    # A think-only agent with no schema to fill gets no tools at all: it writes Markdown.
    if think and not kept:
        return ({**body, "tools": []}, [(t or {}).get("name") or "" for t in tools]) if tools else (body, [])
    if not kept or len(kept) == len(tools):
        return body, []
    dropped = [(t or {}).get("name") or "" for t in tools if t not in kept]
    return {**body, "tools": kept}, dropped


# The most of one tool result a graph subagent's conversation keeps. Measured: graph
# subagents printed whole spreadsheets and PDFs into their own context, 108 requests ran
# past the 131k window, and each backup agent did the same thing again.
RESULT_CHARS = 16_000


def trim_results(body: dict) -> tuple[dict, int]:
    """A graph subagent's request with each oversized tool result cut to head and tail."""
    cut = 0
    messages = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            messages.append(message)
            continue
        blocks = []
        for block in content:
            text = block.get("content") if isinstance(block, dict) and block.get("type") == "tool_result" else None
            if isinstance(text, list):
                text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
            if isinstance(text, str) and len(text) > RESULT_CHARS:
                cut += 1
                head, tail = text[: RESULT_CHARS * 2 // 3], text[-RESULT_CHARS // 4:]
                block = {**block, "content": (
                    f"{head}\n\n[... {len(text) - len(head) - len(tail):,} characters cut. Print "
                    f"less: counts, head, a few rows, or the lines you need ...]\n\n{tail}")}
            blocks.append(block)
        messages.append({**message, "content": blocks})
    return ({**body, "messages": messages}, cut) if cut else (body, 0)


_CODE_PATH = re.compile(r"\b[\w./-]+\.(py|js|mjs|jsx|ts|tsx|r|sh|go|java|kt|rb|rs|c|h|cpp|cs|swift|php)\b", re.I)
_CODE_WORK = re.compile(r"\b(implement|bugs?|fix|fixes|refactor|function|functions|feature|tests?)\b", re.I)


def _has_code(cwd: str | None) -> bool:
    if not cwd or not os.path.isdir(cwd):
        return False
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        if root[len(cwd):].count(os.sep) > 1:
            dirs[:] = []
        if any(_CODE_PATH.search(f) for f in files):
            return True
    return False


def suits(task: str, cwd: str | None) -> bool:
    """Whether a task is one the graph does better than Agentaus's own workflow.

    Measured: the graph took a spreadsheet analysis from 0.61 to 1.00 and a tender response
    from 0.71 to 0.80, but on code it lost - ten functions in one file scored 0.30-0.50
    through the graph and 0.80 through Agentaus's own script. So code work stays with the
    model's own workflow unless AGENTAUS_GRAPH_FOR_CODE says otherwise.
    """
    # The dynamic graph plans the shape of each task, and code is where it gains most.
    if settings.agentaus_graph_kind == "dynamic" or settings.agentaus_graph_for_code:
        return True
    text = task or ""
    if _CODE_PATH.search(text):
        return False
    return not (_CODE_WORK.search(text) and _has_code(cwd))


# Roles that read the project but never write to it. A solver or merger that edited the real
# file directly - measured on ten functions - left no candidate to compare and nothing for
# the assembler to swap in.
READ_ONLY_ROLES = {"scout", "plan", "review-plan", "solve", "merge", "check"}
# The harness indents every line of an agent's prompt, so the heading arrives as
# "  ## Your skill: solve". Measured: anchored at column 0 this never matched, no role was
# ever read-only, and scouts, solvers and checkers edited the live project.
_ROLE = re.compile(r"^[ \t>]*#{1,3}\s*Your skill:\s*([\w-]+)", re.M)
_SOURCE = re.compile(r"\.(py|js|mjs|cjs|jsx|ts|tsx|r|sh|go|java|kt|rb|rs|c|h|cc|cpp|cs|swift|php|sql)$", re.I)


def role_of(body: dict) -> str | None:
    for message in body.get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or [] if isinstance(b, dict)]
        for text in texts:
            if MARK in (text or ""):
                found = _ROLE.search(text)
                if found:
                    return found.group(1)
    return None


def guarded_writes(calls: list, body: dict, cwd: str | None) -> list:
    """(call, correction) for each write a graph agent may not make.

    A read-only role writing anywhere in the project, and any role overwriting an existing
    source file wholesale - an assembler once replaced `bank.py` with a merger's
    reconstruction of it, and the module stopped running.
    """
    if not cwd or not is_graph_task(body):
        return []
    role = role_of(body)
    root = os.path.normpath(cwd) + os.sep
    out = []
    for call in calls:
        name = call.get("name")
        if name not in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
            continue
        args = call.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                continue
        if name == "Bash":
            command = (args or {}).get("command") if isinstance(args, dict) else None
            if role in READ_ONLY_ROLES and isinstance(command, str) and bash_writes_into(command, cwd):
                out.append((call, "Your command was not run: it would write into the project "
                                  f"folder, which is read-only for your role ({role}). Work in "
                                  "your own copy under /tmp, as your task says."))
            continue
        path = (args or {}).get("file_path") or (args or {}).get("notebook_path") or ""
        inside = isinstance(path, str) and os.path.normpath(path).startswith(root)
        if inside and role in READ_ONLY_ROLES:
            out.append((call, f"Your call to `{name}` was not run: the project folder is read-only "
                              f"for your role ({role}). Work in your own copy, as your task says."))
        elif inside and name == "Write" and _SOURCE.search(path) and os.path.exists(path):
            out.append((call, f"Your call to `Write` was not run: `{path}` is an existing source "
                              "file and is never rewritten whole. Change it with small `Edit` "
                              "calls, or run the commands you were given."))
    return out


# --- What a shell command writes ----------------------------------------------------------
#
# Measured: with Write and Edit blocked, read-only roles wrote into the project through Bash
# instead - `sed -i`, `apply_patch`, `cat > file`, python `open(..., 'w')`, `cp x tools/bank.py`
# - mostly with relative paths, since a subagent's shell starts in the project folder. This
# reads a command well enough to say where it writes. A false block costs the agent one
# retry with the correction in hand; a miss costs a corrupted project.

_WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "nice", "xargs", "builtin"}
_GIT_WRITES = {"apply", "checkout", "restore", "reset", "stash", "clean", "am", "switch", "rm",
               "mv", "cherry-pick", "revert", "merge", "rebase", "pull"}
_PY_WRITE = re.compile(
    r"""open\([^\n]*?['"](?:[wax]b?\+?|[wax]t|r\+b?|rb\+)['"]|\.write_(?:text|bytes)\("""
    r"""|\.to_(?:csv|excel|json|parquet)\(|json\.dump\(|\.save\(|\.savefig\("""
    r"""|shutil\.(?:copy\w*|move|rmtree)\(|os\.(?:remove|unlink|rename|replace)\(""")
_PY_LITERAL = re.compile(r"""(?:[rbuRBU]{0,2})(['"])((?:\\.|(?!\1).)*?)\1""")
_NOT_PATHS = {"w", "a", "x", "wb", "ab", "xb", "w+", "a+", "r+", "wt", "at", "utf-8", "utf8",
              "", "\\n"}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$|^\*\*\* Move to:\s*(.+?)\s*$", re.M)


def _heredocs(command: str) -> tuple[str, list]:
    """The command with each heredoc body taken out, and the bodies."""
    bodies = []
    opener = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
    while True:
        m = opener.search(command)
        if not m:
            return command, bodies
        nl = command.find("\n", m.end())
        if nl < 0:
            return command[:m.start()] + command[m.end():], bodies
        end = re.compile(rf"^[ \t]*{re.escape(m.group(2))}[ \t]*$", re.M).search(command, nl + 1)
        stop, resume = (end.start(), end.end()) if end else (len(command), len(command))
        bodies.append(command[nl + 1:stop])
        command = command[:m.start()] + command[m.end():nl + 1] + command[resume:]


def _newlines_to_separators(command: str) -> str:
    out, quote, escaped = [], None, False
    for ch in command:
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
            continue
        out.append(ch)
    return "".join(out)


def _tokens(command: str) -> list:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quotes: read it crudely rather than not at all.
        return re.findall(r"&&|\|\||[;|&()]|>>|&>|>&|>|<|[^\s;|&()<>]+", command)


def _punct(token: str) -> bool:
    return bool(token) and all(c in "();<>|&" for c in token)


def _norm(path: str) -> str:
    path = os.path.normpath(path)
    # macOS: /tmp and /var are /private/tmp and /private/var under another name.
    return path[len("/private"):] if re.match(r"^/private/(tmp|var)(/|$)", path) else path


def _resolve(target: str, here: str, env: dict) -> str | None:
    def var(m):
        name = m.group(1) or m.group(2)
        return {"PWD": here, "HOME": os.path.expanduser("~")}.get(name, env.get(name, "\0"))
    target = re.sub(r"\$\{(\w+)\}|\$(\w+)", var, target)
    if "\0" in target or "$" in target or not target:
        return None   # a variable it set somewhere this cannot see: not counted
    target = os.path.expanduser(target)
    return _norm(target if os.path.isabs(target) else os.path.join(here, target))


def _options_and_args(words: list, takes_value=()) -> tuple[set, list]:
    opts, args, skip = set(), [], False
    for k, w in enumerate(words):
        if skip:
            skip = False
            continue
        if w.startswith("-") and len(w) > 1:
            opts.add(w)
            skip = w in takes_value
        else:
            args.append(w)
    return opts, args


def _command_writes(words: list, here: str, bodies: list, env: dict) -> list:
    """The raw write targets of one simple command (no redirects); "." means where it runs."""
    while words and (words[0] in _WRAPPERS or re.match(r"^\w+=", words[0])
                     or (words[0].startswith("-") and len(words[0]) > 1)):
        word = words.pop(0)
        if re.match(r"^\w+=", word):
            env[word.split("=", 1)[0]] = word.split("=", 1)[1]
    if not words:
        return []
    name, rest = os.path.basename(words[0]), words[1:]
    if name == "export":
        for word in rest:
            if "=" in word:
                env[word.split("=", 1)[0]] = word.split("=", 1)[1]
        return []
    if name in ("sed", "perl") and any(w == "--in-place" or re.match(r"^-(?!M)[a-zA-Z]*i", w) for w in rest):
        files, script_given, skip = [], False, False
        for k, w in enumerate(rest):
            if skip:
                skip = False
                continue
            if w in ("-e", "-f", "--expression", "--file"):
                script_given, skip = True, True
            elif w == "" or w.startswith("-"):
                continue
            else:
                files.append(w)
        return files if script_given else files[1:]
    if name == "tee":
        return _options_and_args(rest)[1]
    if name in ("mv", "rm", "touch", "truncate", "unlink", "rmdir", "shred"):
        return _options_and_args(rest, takes_value={"-s", "-t"})[1]
    if name in ("cp", "install", "ln"):
        if "-t" in rest and rest.index("-t") + 1 < len(rest):
            return [rest[rest.index("-t") + 1]]
        args = _options_and_args(rest, takes_value={"-m", "-o", "-g", "-S"})[1]
        return args[-1:] if len(args) >= 2 else []
    if name == "apply_patch":
        found = [a or b for body in bodies + [" ".join(rest)] for a, b in _PATCH_FILE.findall(body)]
        return found or ["."]
    if name == "patch":
        if "-d" in rest and rest.index("-d") + 1 < len(rest):
            return [rest[rest.index("-d") + 1]]
        if "-o" in rest and rest.index("-o") + 1 < len(rest):
            return [rest[rest.index("-o") + 1]]
        args = _options_and_args(rest, takes_value={"-i", "-p", "-r", "-B", "-z", "-F"})[1]
        return args[:1] or ["."]
    if name == "git":
        base, k = ".", 0
        while k < len(rest) and rest[k].startswith("-"):
            if rest[k] == "-C" and k + 1 < len(rest):
                base, k = rest[k + 1], k + 1
            k += 1
        sub = rest[k] if k < len(rest) else ""
        if sub == "stash" and k + 1 < len(rest) and rest[k + 1] in ("list", "show"):
            return []
        return [base] if sub in _GIT_WRITES else []
    if name == "find":
        out = []
        if "-delete" in rest:
            out += [w for w in rest if not w.startswith("-")][:1] or ["."]
        for k, w in enumerate(rest):
            if w in ("-exec", "-execdir", "-ok"):
                out += _command_writes(list(rest[k + 1:]), here, bodies, env)
        return out
    if name in ("bash", "sh", "zsh") and "-c" in rest and rest.index("-c") + 1 < len(rest):
        return [("abs", t) for t in _bash_targets(rest[rest.index("-c") + 1], here, env)]
    if re.match(r"^(python|pypy)[\d.]*$", name):
        code = [rest[rest.index("-c") + 1]] if "-c" in rest and rest.index("-c") + 1 < len(rest) else []
        return _python_writes("\n".join(code + bodies))
    return []


def _python_writes(code: str) -> list:
    out = []
    for line in code.splitlines():
        if not _PY_WRITE.search(line):
            continue
        literals = [v for _, v in _PY_LITERAL.findall(line)
                    if v not in _NOT_PATHS and "{" not in v and ("/" in v or "." in v)]
        absolute = [v for v in literals if v.startswith(("/", "~"))]
        if absolute or literals:
            out += absolute or literals
            continue
        # A path held in a variable: the absolute paths the code names, or where it runs.
        named = [v for _, v in _PY_LITERAL.findall(code) if v.startswith("/") and "{" not in v]
        out += named or ["."]
    return out


def _bash_targets(command: str, start: str, env: dict | None = None) -> list:
    """Every path a shell command would write to, resolved, in the order found."""
    env = {} if env is None else env
    text, bodies = _heredocs(command)
    tokens = _tokens(_newlines_to_separators(text))
    here, targets, segment = start, [], []

    def flush():
        nonlocal here
        words, redirects, k = [], [], 0
        while k < len(segment):
            token = segment[k]
            if _punct(token) and ">" in token:
                nxt = segment[k + 1] if k + 1 < len(segment) else ""
                if words and words[-1].isdigit():
                    words.pop()   # the 2 of `2>/dev/null` is not an argument
                if not (token.endswith("&") and re.fullmatch(r"\d+|-", nxt)) and nxt:
                    redirects.append(nxt)
                k += 2
                continue
            if _punct(token) and "<" in token:
                k += 2
                continue
            words.append(token)
            k += 1
        if words and words[0] in ("cd", "pushd"):
            dest = words[1] if len(words) > 1 and words[1] != "-" else "~"
            here = _resolve(dest, here, env) or here
            segment.clear()
            return
        raw = _command_writes(list(words), here, bodies, env) + redirects
        for t in raw:
            if isinstance(t, tuple):
                targets.append(t[1])
                continue
            if t in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"):
                continue
            resolved = _resolve(t, here, env)
            if resolved:
                targets.append(resolved)
        segment.clear()

    for token in tokens:
        if _punct(token) and ">" not in token and "<" not in token:
            flush()
        else:
            segment.append(token)
    flush()
    return targets


def bash_writes_into(command: str, cwd: str) -> list:
    """The paths inside cwd that a shell command, started in cwd, would write to."""
    root = _norm(cwd)
    try:
        targets = _bash_targets(command, root)
    except Exception as exc:  # noqa: BLE001 - an unreadable command is not blocked
        log.debug("could not read a shell command (%s)", exc)
        return []
    return [t for t in targets if t == root or t.startswith(root.rstrip("/") + "/")]


# --- Agents the graph has stopped waiting for ---------------------------------------------
#
# The graph moves on after a deadline, but the agent it gave up on keeps looping. Measured:
# orphaned agents kept editing files 15+ minutes after the graph stopped waiting, some of it
# after the assembler had put the chosen changes in place. Past its limit, an agent's next
# request goes upstream with no tools and an instruction to report, so it ends.

TIME_UP = ("**Time is up.** Stop working now and reply with your result, in the Markdown your "
           "skill describes, from what you have already found.")
# The roles that write the deliverables get longer: they run once, after everything else.
LONG_ROLES = {"assemble", "fix"}
_FIRST_SEEN: "OrderedDict[str, list]" = OrderedDict()
_FIRST_SEEN_LIMIT = 4096


def _first_user_text(body: dict) -> str:
    for message in body.get("messages") or []:
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else "\n".join(
                b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
    return ""


def agent_limit(body: dict) -> int:
    if role_of(body) in LONG_ROLES:
        return settings.agentaus_graph_writer_max_s
    return settings.agentaus_graph_agent_max_s


def overdue(body: dict, now: float | None = None) -> tuple[float, bool]:
    """(how far past its limit this graph agent is - 0 when not, whether that is news).

    An agent is known by its opening prompt. Its first request - nothing but that prompt -
    starts the clock again, so a retry launched with the same prompt gets its own time.
    """
    now = time.monotonic() if now is None else now
    key = hashlib.sha256(_first_user_text(body).encode()).hexdigest()
    fresh = len(body.get("messages") or []) <= 1
    entry = _FIRST_SEEN.get(key)
    if entry is None or fresh:
        entry = _FIRST_SEEN[key] = [now, False]
    _FIRST_SEEN.move_to_end(key)
    while len(_FIRST_SEEN) > _FIRST_SEEN_LIMIT:
        _FIRST_SEEN.popitem(last=False)
    over = now - entry[0] - agent_limit(body)
    if over <= 0:
        return 0.0, False
    news, entry[1] = not entry[1], True
    return over, news


def time_up(body: dict) -> dict:
    """The request with no tools and TIME_UP as the last thing the user said.

    Appended to the last user message when there is one - after a tool result it is one -
    so the conversation never has two user turns in a row.
    """
    messages = list(body.get("messages") or [])
    block = {"type": "text", "text": TIME_UP}
    last = messages[-1] if messages else None
    trimmed = {k: v for k, v in body.items() if k not in ("tools", "tool_choice")}
    if last and TIME_UP in json.dumps(last.get("content"), ensure_ascii=False):
        return trimmed   # a refit of a request already told
    if last and last.get("role") == "user":
        content = last.get("content")
        content = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        messages[-1] = {**last, "content": content + [block]}
    else:
        messages.append({"role": "user", "content": [block]})
    return {**trimmed, "messages": messages}

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

from . import throttle
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


def total_ms(started_at: float | None = None, now: float | None = None) -> int:
    """The graph's hard end (args.total_ms): AGENTAUS_GRAPH_TOTAL_S, less the time the session
    has already spent when its start is known. Measured (d12-d14): graphs started 10-95 s into
    their session, and the harness kills a run at 7,200 s from the session's start."""
    total = settings.agentaus_graph_total_s
    if started_at:
        total -= max(0.0, (time.time() if now is None else now) - started_at)
    return int(max(60.0, total) * 1000)


def expand(call: dict, task: str, cwd: str | None, started_at: float | None = None) -> dict:
    """`call` with the graph in place of the script Agentaus wrote, or unchanged.

    `started_at` is when the session began (time.time()), if the bridge saw it."""
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
                 # The graph waits a little longer than the bridge lets an agent run, so the
                 # result an agent is made to report at TIME_UP still counts. Measured: the
                 # cap was 720 s and the graph's deadline 600 s - every forced report arrived
                 # after the graph had stopped listening.
                 "deadline_ms": (settings.agentaus_graph_agent_max_s + 120) * 1000,
                 # Solve, merge and check together. Measured (d12): on the 3,600 s default
                 # workflow 1 ran 4,076-4,525 s, and with the main loop after it a run did
                 # not fit inside 7,200 s.
                 "budget_ms": settings.agentaus_graph_budget_s * 1000,
                 # The hard end for the whole graph: it must return a result before this.
                 "total_ms": total_ms(started_at),
                 # A writer's limit as the bridge enforces it, plus the same 120 s margin.
                 "writer_ms": (settings.agentaus_graph_writer_max_s + 120) * 1000,
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


# A question the request's documents ask: `Q3. ...`, `**Q3.** ...`, `- Q3: ...`, and the heading
# forms `## Q3 - title` / `### Q3` with the question on the line below. Measured: the graph
# read the question numbers off the planner's Task, which said "Q1, Q2, ... Q9" - so Q3 to
# Q8 were never planned, though the RFQ listed all nine, one per line.
_QUESTION = re.compile(r"^[ \t>*#-]*\**Q(\d{1,2})\**[.:)]\**\s+(\S.{6,400}?)\s*$", re.M)
_QUESTION_HEADING = re.compile(
    r"^[ \t>]*#{1,6}[ \t]*\**Q(\d{1,2})\b\**[ \t]*(?:[-\u2010-\u2015:.)]\**[ \t]*)?(.*?)[ \t]*$", re.M)
_MAX_QUESTIONS = 40
_QUESTION_FILES = 200
_QUESTION_BYTES = 2 * 1024 * 1024


def _question_text(text: str) -> str:
    return re.sub(r"^\*+|\*+$", "", text.strip()).strip()


def _questions_in(text: str) -> list:
    """(n, question, line) for each question line in one text, in the order they appear."""
    found = []
    for m in _QUESTION.finditer(text):
        found.append((m.start(), int(m.group(1)), _question_text(m.group(2))))
    for m in _QUESTION_HEADING.finditer(text):
        title = _question_text(m.group(2))
        if len(title) < 7:
            # `### Q3` with the question itself on the next line that is not a heading.
            rest = text[m.end():].lstrip("\n").split("\n", 3)
            nxt = next((l.strip() for l in rest[:3] if l.strip()), "")
            if nxt and not nxt.startswith("#") and not _QUESTION.match(nxt):
                title = f"{title} - {_question_text(nxt)}" if title else _question_text(nxt)
        if len(title) >= 7:
            found.append((m.start(), int(m.group(1)), title[:400]))
    return [(n, q, text.count("\n", 0, at) + 1) for at, n, q in sorted(found)]


def _questions(cwd: str, files: list[str], texts: list) -> list:
    """Every numbered question in the project's text files and document copies.

    Shallowest file first, so the request's own RFQ at the top of the project wins over a
    past response in a subfolder; for each number, the first seen wins.
    """
    sources = [(f, os.path.join(cwd, f)) for f in files if f.lower().endswith((".md", ".txt"))]
    sources += [(t["source"], t["text"]) for t in texts]
    sources.sort(key=lambda s: (s[0].count(os.sep), s[0]))
    found: dict = {}
    for rel, path in sources[:_QUESTION_FILES]:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(_QUESTION_BYTES)
        except OSError:
            continue
        for n, question, line in _questions_in(text):
            if n not in found:
                found[n] = {"n": n, "text": f"Q{n}. {question}", "file": rel, "line": line}
    return [found[k] for k in sorted(found)][:_MAX_QUESTIONS]


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


PREVIOUS_SCRATCH = 2


def _set_aside(scratch: str) -> None:
    """Rename an earlier run's scratch folder to `<scratch>.prev<epoch>`, keeping the newest
    PREVIOUS_SCRATCH of them. Measured (d12 he10, c1913h): a second graph deleted the first
    one's solver copies and its pristine snapshot, so the run could not be diagnosed and the
    second snapshot was taken of the first graph's output."""
    parent, base = os.path.split(scratch)
    prefix = base + ".prev"

    def order(name: str) -> tuple:
        epoch, _, n = name[len(prefix):].partition("_")
        return (int(epoch) if epoch.isdigit() else 0, int(n) if n.isdigit() else 0)

    def earlier() -> list:
        try:
            return sorted((e for e in os.listdir(parent or ".") if e.startswith(prefix)), key=order)
        except OSError:
            return []

    if os.path.exists(scratch):
        epoch = int(time.time())
        # Two runs in one second get `_1`, `_2`: always after the ones already set aside.
        same = [order(e)[1] for e in earlier() if order(e)[0] == epoch]
        name = f"{prefix}{epoch}" + (f"_{max(same) + 1}" if same else "")
        os.replace(scratch, os.path.join(parent, name))
    kept = earlier()
    for name in kept[:-PREVIOUS_SCRATCH] if len(kept) > PREVIOUS_SCRATCH else []:
        path = os.path.join(parent, name)
        _make_writable(path)
        shutil.rmtree(path, ignore_errors=True)


def prepare(cwd: str | None) -> dict:
    """The args the bridge adds for the dynamic graph: pristine, texts, questions, functions,
    constants, examples. Best effort - a step that fails is logged and left out, never raised."""
    extra: dict = {"pristine": "", "texts": [], "questions": [], "functions": [], "constants": [],
                   "examples": ""}
    if not cwd or not os.path.isdir(cwd):
        return extra
    scratch = scratch_of(cwd)
    started = time.monotonic()
    try:
        _set_aside(scratch)
        os.makedirs(scratch, exist_ok=True)
        files = _project_files(cwd)
    except Exception as exc:  # noqa: BLE001
        log.warning("graph prep: could not set up %s (%s)", scratch, exc)
        return extra
    steps = (
        ("pristine", lambda: _snapshot(cwd, files, os.path.join(scratch, "pristine"))),
        ("texts", lambda: _texts(cwd, files, os.path.join(scratch, "inputs"))),
        ("questions", lambda: _questions(cwd, files, extra["texts"])),
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
    log.info("graph prep: %s in %.1fs - snapshot %s, %d document(s), %d question(s), "
             "%d function(s), %d file(s) of constants", scratch, time.monotonic() - started,
             "yes" if extra["pristine"] else "no", len(extra["texts"]), len(extra["questions"]),
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
# "0/0 pass" read as a pass. Measured: 26 agents ran this on a module with no `>>>` lines and
# went on as if their change were tested.
NONE = "no docstring examples - write the docstring's prose examples as asserts yourself"
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
    print(f'0/{len(cases)} docstring examples pass' if cases else NONE)
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
print(f'{n - bad}/{n} docstring examples pass' if n else NONE)
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


def is_graph_script(script) -> bool:
    """Whether a Workflow script is the bridge's own graph."""
    return isinstance(script, str) and bool(script) and script in (SCRIPT, DYNAMIC_SCRIPT)


# The main loop's stand-in for the graph script in its own history. See server._without_graph_script.
SCRIPT_STAND_IN = "agentaus-solve"


# --- One graph per session, remembered past compaction ---------------------------------------
#
# Measured (d12 he10, c1913h): compaction summarised the first Workflow call away, the guards
# read the compacted messages and decided no Workflow had run, 40 correct Edits were refused
# with "Call `Workflow` first", and the model launched a second graph - on a 0-char task, since
# the request had been summarised away too. So each graph substitution is remembered here, per
# session, with its task, and what the client's messages show about it is folded in.

_RUNS: "OrderedDict[str, dict]" = OrderedDict()
_RUNS_LIMIT = 256
_TASK_ID = re.compile(r"Task ID:\s*([\w-]+)")
_REPORTED = re.compile(r"<task-id>\s*([\w-]+)\s*</task-id>")
_NO_ID = "?"


def run_key(body: dict) -> str | None:
    """The session a main loop's request belongs to: Claude Code's `metadata.user_id` (it names
    the session) and the working directory. None when neither is known."""
    from .augment import working_directory_of
    user = str(((body or {}).get("metadata") or {}).get("user_id") or "")
    cwd = working_directory_of(body)
    cwd = os.path.normpath(cwd) if cwd else ""
    return f"{user}\x00{cwd}" if user or cwd else None


def _keep(key: str, entry: dict) -> dict:
    _RUNS[key] = entry
    _RUNS.move_to_end(key)
    while len(_RUNS) > _RUNS_LIMIT:
        _RUNS.popitem(last=False)
    return entry


# The record also goes to the graph's scratch folder, so a bridge restarted mid-run still knows
# the graph ran. Without it the run state reads "none" once Claude Code's own compaction has
# dropped the Workflow call, and the "Call Workflow first" refusals (d12) come back.
SESSION_FILE = "session.json"
_WRITTEN: set = set()


def _session_path(key: str) -> str | None:
    cwd = key.split("\x00", 1)[1] if "\x00" in key else ""
    return os.path.join(scratch_of(cwd), SESSION_FILE) if cwd else None


def _persist(key: str, entry: dict) -> None:
    path = _session_path(key)
    if not path or not os.path.isdir(os.path.dirname(path)):
        return   # no graph has prepared this folder: nothing to come back to
    try:
        try:
            with open(path, encoding="utf-8") as fh:
                stored = json.load(fh)
        except (OSError, ValueError):
            stored = {}
        stored = stored if isinstance(stored, dict) else {}
        stored[key] = {**entry, "launched": sorted(entry["launched"]),
                       "reported": sorted(entry["reported"])}
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(stored, fh)
        os.replace(tmp, path)
        _WRITTEN.add(path)
    except OSError as exc:
        log.warning("graph session record not saved to %s (%s)", path, exc)


def _restore(key: str) -> dict | None:
    path = _session_path(key)
    try:
        with open(path or "", encoding="utf-8") as fh:
            stored = (json.load(fh) or {}).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(stored, dict) or "task" not in stored:
        return None
    entry = {**stored, "launched": set(stored.get("launched") or []),
             "reported": set(stored.get("reported") or [])}
    log.info("graph session record restored from %s", path)
    return _keep(key, entry)


def remember_run(body: dict, task: str, said: str | None) -> None:
    """Record that the graph was put in place of this session's Workflow call."""
    key = run_key(body)
    if key:
        _persist(key, _keep(key, {"task": task, "said": said, "at": time.time(),
                                  "launched": set(), "reported": set(), "found_at": None}))


def forget_runs() -> None:
    """Clear every session's record, and the record files this process wrote. For tests: it is
    module state."""
    _RUNS.clear()
    _STARTED.clear()
    for path in list(_WRITTEN):
        try:
            os.remove(path)
        except OSError:
            pass
        _WRITTEN.discard(path)


# When the bridge first saw each session's main loop (time.time()), for args.total_ms.
_STARTED: "OrderedDict[str, float]" = OrderedDict()


def session_started(body: dict) -> float | None:
    key = run_key(body)
    return _STARTED.get(key) if key else None


def _texts_in(content) -> list:
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def _result_text(block: dict) -> str:
    text = block.get("content")
    if isinstance(text, list):
        return "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return text if isinstance(text, str) else json.dumps(text)


def note_run(body: dict) -> dict | None:
    """This session's graph record, updated with what the client's messages show: graph
    launches ("Workflow launched in background") and their `<task-notification>` reports.

    Read the body as the client sent it, never a compacted one. A launch seen with no record
    (the bridge restarted) starts one, with the task the graph call carried; its `found_at` is
    the message index of that call, so the caller can fill `said`.

    None for a subagent: the graph's own agents share the session's `user_id` and working
    directory, and its assembler must write while the graph runs.
    """
    from .augment import is_subagent_turn
    key = run_key(body)
    if not key or is_subagent_turn(body):
        return None
    if key not in _STARTED:
        _STARTED[key] = time.time()
        while len(_STARTED) > _RUNS_LIMIT:
            _STARTED.popitem(last=False)
    entry = _RUNS.get(key) or _restore(key)
    uses: dict = {}
    launched, reported = set(), set()
    first_use = None
    for index, message in enumerate(body.get("messages") or []):
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == "Workflow"
                        and is_graph_script((block.get("input") or {}).get("script"))):
                    uses[block.get("id")] = (index, ((block.get("input") or {}).get("args") or {}).get("task") or "")
            continue
        if message.get("role") != "user":
            continue
        for block in content if isinstance(content, list) else []:
            if (isinstance(block, dict) and block.get("type") == "tool_result"
                    and block.get("tool_use_id") in uses and not block.get("is_error")):
                text = _result_text(block)
                if re.search(r"Workflow launched in (the )?background", text, re.I):
                    launched.update(_TASK_ID.findall(text) or [_NO_ID])
                    if first_use is None:
                        first_use = uses[block.get("tool_use_id")]
        for text in _texts_in(content):
            if "<task-notification>" in (text or ""):
                reported.update(_REPORTED.findall(text))
                if launched:
                    reported.add(_NO_ID)
    if entry is None:
        if not launched:
            return None
        entry = _keep(key, {"task": first_use[1], "said": None, "at": time.time(),
                            "launched": set(), "reported": set(), "found_at": first_use[0]})
    before = (len(entry["launched"]), len(entry["reported"]))
    entry["launched"] |= launched
    entry["reported"] |= reported & (entry["launched"] | {_NO_ID})
    if (len(entry["launched"]), len(entry["reported"])) != before:
        _persist(key, entry)
    return entry


def run_state(entry: dict | None) -> str:
    """"none", "substituted" (not yet seen launched), "running" or "reported"."""
    if not entry:
        return "none"
    if not entry["launched"]:
        return "substituted"
    if entry["launched"] - entry["reported"] - {_NO_ID} or (
            _NO_ID in entry["launched"] and _NO_ID not in entry["reported"]):
        return "running"
    return "reported"


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


NO_APPLY_PATCH = ("`apply_patch` is not installed here. Write the file you were given instead "
                  "(it holds only your function), or use small `Edit`s.")
_APPLY_PATCH = re.compile(r"\s*(?:cd\s+[^;&|\n]+?\s*(?:&&|;)\s*)?apply_patch\b")


def guarded_writes(calls: list, body: dict, cwd: str | None) -> list:
    """(call, correction) for each write a graph agent may not make.

    A read-only role writing anywhere in the project, and any role overwriting an existing
    source file wholesale - an assembler once replaced `bank.py` with a merger's
    reconstruction of it, and the module stopped running.
    """
    if not is_graph_task(body):
        return []
    role = role_of(body)
    root = os.path.normpath(cwd) + os.sep if cwd else None
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
            # Measured (d12 he10): solvers ran `apply_patch` again and again - exit 127 each
            # time, and one hung as a background task.
            if isinstance(command, str) and _APPLY_PATCH.match(command):
                out.append((call, NO_APPLY_PATCH))
                continue
            if not root:
                continue
            if role in READ_ONLY_ROLES and isinstance(command, str) and bash_writes_into(command, cwd):
                out.append((call, "Your command was not run: it would write into the project "
                                  f"folder, which is read-only for your role ({role}). Work in "
                                  "your own copy under /tmp, as your task says."))
            # Measured (he10, d9): an assembler blocked from `Write` seven times ran
            # `cp <solver copy>/solutions.py <project>/solutions.py` instead, and one solver's
            # file replaced nine good functions.
            elif role in LONG_ROLES and isinstance(command, str) and overwrites_source(command, cwd):
                out.append((call, "Your command was not run: it replaces an existing source file "
                                  "in the project whole. Run the commands you were given, or make "
                                  "small `Edit` calls."))
            continue
        if not root:
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


_COPIERS = ("cp", "install", "ln", "rsync", "ditto")
_COPY_VALUES = {"-m", "-o", "-g", "-S"}
_RSYNC_VALUES = {"-e", "-f", "--exclude", "--include", "--filter", "--exclude-from",
                 "--include-from", "--files-from", "--rsh", "--backup-dir", "--suffix",
                 "--log-file", "--chmod", "--chown", "--rsync-path"}


def _copy_parts(name: str, rest: list) -> tuple[list, list]:
    """(sources, [destination]) of a cp/mv/install/ln/rsync/ditto command, as written."""
    values = _RSYNC_VALUES if name == "rsync" else _COPY_VALUES
    if "-t" in rest and rest.index("-t") + 1 < len(rest):
        dest = rest[rest.index("-t") + 1]
        return _options_and_args(rest, takes_value=values | {"-t"})[1], [dest]
    args = _options_and_args(rest, takes_value=values)[1]
    if name == "rsync":
        args = [a for a in args if not re.match(r"^[^/]*:", a)]   # host:path is not here
    return (args[:-1], args[-1:]) if len(args) >= 2 else ([], [])


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
    if name in _COPIERS:
        return _copy_parts(name, rest)[1][-1:]
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


def _whole_writes(words: list, here: str, env: dict) -> list:
    """The files one simple command replaces whole - cp/mv/rsync/install/ditto onto them, or
    tee - resolved. A directory copied into a directory replaces each file it holds."""
    while words and (words[0] in _WRAPPERS or re.match(r"^\w+=", words[0])
                     or (words[0].startswith("-") and len(words[0]) > 1)):
        word = words.pop(0)
        if re.match(r"^\w+=", word):
            env[word.split("=", 1)[0]] = word.split("=", 1)[1]
    if not words:
        return []
    name, rest = os.path.basename(words[0]), words[1:]
    if name in ("bash", "sh", "zsh") and "-c" in rest and rest.index("-c") + 1 < len(rest):
        return _bash_targets(rest[rest.index("-c") + 1], here, env, whole=True)
    if name == "tee":
        if any(w in ("-a", "--append") for w in rest):
            return []
        return [t for t in (_resolve(w, here, env) for w in _options_and_args(rest)[1]) if t]
    if name not in _COPIERS + ("mv",):
        return []
    sources, dest = _copy_parts(name, rest)
    target = _resolve(dest[0], here, env) if dest else None
    if not target:
        return []
    if not os.path.isdir(target):
        return [target]
    out = []
    for raw in sources:
        src = _resolve(raw, here, env)
        if not src:
            continue
        if not os.path.isdir(src):
            out.append(os.path.join(target, os.path.basename(src)))
            continue
        # `cp -R a/. b/`, `rsync a/ b/` and ditto copy a folder's contents; `cp -R a b/` copies
        # the folder into b.
        into = target if (raw.endswith(("/.", "/")) or name == "ditto") else os.path.join(
            target, os.path.basename(src))
        seen = 0
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for f in files:
                out.append(os.path.join(into, os.path.relpath(os.path.join(root, f), src)))
                seen += 1
            if seen > _SNAPSHOT_FILES:
                break
    return out


def _bash_targets(command: str, start: str, env: dict | None = None, whole: bool = False) -> list:
    """Every path a shell command would write to, resolved, in the order found.

    `whole`: only the files it replaces whole - a copy onto them, `tee`, or a `>` redirect -
    and not the ones it edits in place (`sed -i`, `patch`, a python edit script) or appends to.
    """
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
                if not (token.endswith("&") and re.fullmatch(r"\d+|-", nxt)) and nxt \
                        and not (whole and token not in (">", ">|", "&>")):
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
        if whole:
            raw = [("abs", t) for t in _whole_writes(list(words), here, env)] + redirects
        else:
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


def overwrites_source(command: str, cwd: str) -> bool:
    """Whether a shell command replaces an existing source file in cwd whole - `cp x bank.py`,
    `cat > bank.py`, `rsync copy/ project/`. Copies from the graph's own pristine snapshot or
    pre-fix backup (restore, undo) are not counted; neither are `patch` and edit scripts."""
    root = _norm(cwd)
    scratch = _norm(scratch_of(cwd))
    try:
        if re.search(rf"{re.escape(scratch)}/pristine\b|/before-fix\b", _norm_command(command)):
            return False
        targets = _bash_targets(command, root, whole=True)
    except Exception as exc:  # noqa: BLE001 - an unreadable command is not blocked
        log.debug("could not read a shell command (%s)", exc)
        return False
    inside = [t for t in targets if t.startswith(root.rstrip("/") + "/")]
    return any(_SOURCE.search(t) and os.path.isfile(t) for t in inside)


def _norm_command(command: str) -> str:
    return re.sub(r"/private(/(?:tmp|var)/)", r"\1", command)


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


def _agent_key(body: dict) -> tuple[str, str]:
    """(key, title) an agent is known by across all its requests.

    The graph prompt's own title line, the project it works in, and whether it is the
    backup attempt. Measured (he10, d9): keyed on a hash of every text block of the first
    user message, the key changed between an agent's requests - Claude Code adds reminder
    blocks to that message as a run goes on - and agents ran 1100-1300 s with no TIME_UP.
    """
    for message in body.get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or [] if isinstance(b, dict)]
        for text in texts:
            if MARK in (text or ""):
                head = text[text.index(MARK):]
                title = re.search(r"^[ \t>]*#[ \t]+(\S.*?)[ \t]*$", head, re.M)
                folder = re.search(r"\*\*Project folder:\*\*[ \t]*`([^`\n]*)`", head)
                backup = "**Backup attempt:**" in head
                name = title.group(1) if title else head[len(MARK):400].strip()
                key = f"{name}|{folder.group(1) if folder else ''}|{backup}"
                return key, name + (" (backup)" if backup else "")
    text = _first_user_text(body)
    return text, text[:80]


def agent_limit(body: dict) -> int:
    if role_of(body) in LONG_ROLES:
        return settings.agentaus_graph_writer_max_s
    return settings.agentaus_graph_agent_max_s


def overdue(body: dict, now: float | None = None) -> tuple[float, bool]:
    """(how far past its limit this graph agent is - 0 when not, whether that is news).

    An agent is known by its prompt's title (see _agent_key). Its first request - nothing
    but that prompt - starts the clock again, so a retry launched with the same prompt gets
    its own time. Call it with the request as received, before fitting or compaction.

    Only time the upstream could serve counts: seconds it was down (throttle.Health) are left
    out. Measured (d14): agents hit their 720 s limit during an outage, were told "time is
    up", and the time-up request itself got a 524 - half-done answers, from wall-clock time
    the agent never had.
    """
    now = time.monotonic() if now is None else now
    key, title = _agent_key(body)
    key = hashlib.sha256(key.encode()).hexdigest()
    fresh = not any(m.get("role") == "assistant" for m in body.get("messages") or [])
    entry = _FIRST_SEEN.get(key)
    if entry is None or fresh:
        log.info("graph agent clock started: %s", title[:200])
        entry = _FIRST_SEEN[key] = [now, False, throttle.down_total()]
    _FIRST_SEEN.move_to_end(key)
    while len(_FIRST_SEEN) > _FIRST_SEEN_LIMIT:
        _FIRST_SEEN.popitem(last=False)
    over = _over(entry, body, now)
    if over <= 0:
        return 0.0, False
    news, entry[1] = not entry[1], True
    return over, news


def _over(entry: list, body: dict, now: float) -> float:
    down = max(0.0, throttle.down_total() - (entry[2] if len(entry) > 2 else 0.0))
    return now - entry[0] - down - agent_limit(body)


def seconds_over(body: dict, now: float | None = None) -> float:
    """How far past its limit this graph agent is now (0 when not, or unknown) - like
    `overdue`, but it starts no clock and records nothing: safe to ask between retries."""
    entry = _FIRST_SEEN.get(hashlib.sha256(_agent_key(body)[0].encode()).hexdigest())
    if entry is None:
        return 0.0
    return max(0.0, _over(entry, body, time.monotonic() if now is None else now))


# What the bridge hands back when a time-up turn cannot be answered upstream. Each role's own
# result heading, so the graph reads it where it reads a real reply (skills/*.md).
_RESULT_HEADING = {"solve": "Answer", "merge": "Answer", "check": "Evidence", "fix": "Evidence",
                   "assemble": "Problems", "scout": "What is in them"}
TIME_UP_PARTIAL = "(time up; partial, assembled by the bridge)"
OUT_OF_TIME = "(out of time)"


def time_up_fallback(body: dict, *, text_chars: int = 4000, result_chars: int = 1500,
                     results: int = 3) -> str:
    """A graph agent's result put together from what it already has: its last assistant text
    and its last few tool results, under its role's result heading.

    Measured (d10-d14): "time is up, report now" turns got a 524 20-50% of the time (ordinary
    turns about 0.1%), and the identical doomed prompt was replayed five times and copied ten
    more by Claude Code's fallback. What the agent found is already in its conversation.
    """
    messages = body.get("messages") or []
    said: list = []
    calls: dict = {}
    outputs: list = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant":
            if isinstance(content, str) and content.strip():
                said.append(content.strip())
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    said.append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    args = block.get("input") or {}
                    what = next((str(args[k]) for k in ("command", "file_path", "pattern", "path")
                                 if isinstance(args, dict) and args.get(k)), "")
                    calls[block.get("id")] = f"{block.get('name') or '?'}: {what}".strip(": ")
        elif message.get("role") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out = _tool_result_text(block).strip()
                    if out and TIME_UP not in out:
                        outputs.append((calls.get(block.get("tool_use_id"), "tool"), out))
    heading = _RESULT_HEADING.get(role_of(body) or "", "Answer")
    parts = [TIME_UP_PARTIAL, "", f"## {heading}", ""]
    text = "\n\n".join(said[-3:]).strip()
    if len(text) > text_chars:
        text = "[...]\n" + text[-text_chars:]
    parts.append(text or "Time ran out before this agent wrote a result. What its tools "
                         "returned last is below.")
    if outputs:
        parts += ["", "## What its last tool calls returned", ""]
        for name, out in outputs[-results:]:
            if len(out) > result_chars:
                out = out[:result_chars // 3] + "\n[...]\n" + out[-(result_chars - result_chars // 3):]
            parts += [f"`{name[:200]}`", "```text", out, "```", ""]
    return "\n".join(parts).strip() + "\n"


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


# --- Plain characters in every text write --------------------------------------------------
#
# Measured (tender, d9): the graph's response.md was clean - its normalise step saw to that -
# and then the main loop rewrote it in one `Write` with 14 non-breaking hyphens, 48 narrow
# no-break spaces and 3 NBSPs; "GST‑registered" no longer matched "GST-registered". So every
# text write by a graph agent, or by a main loop that runs the graph, is made plain on its way
# to Claude Code: the hyphens, spaces and brackets a person would have typed.

_PLAIN = [(re.compile("[‐‑‒−]"), "-"),
          (re.compile("[    ]"), " "),
          (re.compile("【([^】\n]{1,80})】"), r"[\1]")]
_PLAIN_FILE = re.compile(r"\.(md|txt|markdown)$", re.I)


def plain(text: str) -> str:
    for pattern, to in _PLAIN:
        text = pattern.sub(to, text)
    return text


def plain_text_writes(calls: list) -> int:
    """Write/Edit/MultiEdit calls on .md/.txt files, with plain characters in what they write.

    In place; returns how many calls changed. `old_string` is never touched - it has to
    match the file as it is.
    """
    changed = 0
    for call in calls:
        if call.get("name") not in ("Write", "Edit", "MultiEdit"):
            continue
        raw = call.get("arguments")
        try:
            args = json.loads(raw or "{}") if isinstance(raw, str) else raw
        except ValueError:
            continue
        if not isinstance(args, dict):
            continue
        path = args.get("file_path") or args.get("path") or ""
        if not isinstance(path, str) or not _PLAIN_FILE.search(path):
            continue
        new = dict(args)
        for key in ("content", "new_string"):
            if isinstance(new.get(key), str):
                new[key] = plain(new[key])
        if isinstance(new.get("edits"), list):
            new["edits"] = [{**e, "new_string": plain(e["new_string"])}
                            if isinstance(e, dict) and isinstance(e.get("new_string"), str) else e
                            for e in new["edits"]]
        # An edit whose only change was the characters made plain would now change nothing.
        if isinstance(new.get("old_string"), str) and new.get("new_string") == new["old_string"]:
            new["new_string"] = args.get("new_string")
        if new == args:
            continue
        call["arguments"] = json.dumps(new) if isinstance(raw, str) else new
        changed += 1
    return changed


# --- Agents whose whole job is one command -------------------------------------------------
#
# Restore, section writers, join, normalise, backup, undo and swaps each send an agent told
# "Run exactly this command - nothing else - then reply with what it printed". Measured (d9
# tender): eight of them took 506 s, one normalise made six more tool calls after its
# command, and a restore's one-word reply was taken for a failure. The graph marks such a
# prompt with EXACT on the line before a single fenced ```bash block, and the bridge plays
# the agent itself: the first request gets the Bash call, the second gets its output back as
# the reply. Nothing goes upstream.

EXACT = "[exact-command]"
_EXACT_BLOCK = re.compile(
    r"^([ \t]*)" + re.escape(EXACT) + r"[ \t]*\n(?:[ \t]*\n)?([ \t]*)```(?:bash|sh)?[ \t]*\n(.*)\n[ \t]*```[ \t]*$",
    re.M | re.S)   # greedy: a section's own ``` fences sit inside its heredoc
EXACT_OUTPUT_CHARS = 4000
EXACT_HEAD_CHARS, EXACT_TAIL_CHARS = 1000, 3000


def _texts_of(message: dict) -> list:
    content = message.get("content")
    return [content] if isinstance(content, str) else [
        b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def exact_command_of(body: dict) -> str | None:
    """The command a graph agent's prompt says to run exactly, or None.

    The harness indents every line of the prompt; the fence's own indent is taken off each
    line, so a heredoc's terminator and a python body come back as the graph wrote them.
    """
    for message in body.get("messages") or []:
        if message.get("role") == "assistant":
            break
        if message.get("role") != "user":
            continue
        for text in _texts_of(message):
            if MARK not in (text or "") or EXACT not in text:
                continue
            m = _EXACT_BLOCK.search(text)
            if not m:
                return None
            indent = m.group(2)
            lines = [l[len(indent):] if l.startswith(indent) else "" if not l.strip() else l
                     for l in m.group(3).split("\n")]
            command = "\n".join(lines).strip("\n")
            return command or None
    return None


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content if isinstance(content, str) else ""


def exact_command(body: dict) -> tuple[str, str] | None:
    """What the bridge answers for an exact-command graph agent, without the model.

    ("run", command) on its first request; ("report", output) on the request carrying that
    command's tool result; None for any other shape, which goes upstream as usual.
    """
    if not is_graph_task(body) or "Bash" not in {(t or {}).get("name") for t in body.get("tools") or []}:
        return None
    command = exact_command_of(body)
    if not command:
        return None
    messages = body.get("messages") or []
    assistants = [k for k, m in enumerate(messages) if m.get("role") == "assistant"]
    if not assistants:
        return "run", command
    # Claude Code appends notices after the tool result as more user messages - an MCP
    # authentication notice, a tools delta. Measured (d10): every follow-up had five messages,
    # none was recognised, and the model took over all 49 exact-command agents - pick lines lost,
    # FAILED lines dropped, a whole command run twice with typos.
    # And it interleaves `system` messages (d11: roles user,system,assistant,user,system).
    later = messages[assistants[0] + 1:] if len(assistants) == 1 else []
    if not later or any(m.get("role") not in ("user", "system") for m in later):
        log.info("graph exact command: follow-up not recognised (roles %s)",
                 ",".join(str(m.get("role")) for m in messages))
        return None
    content = messages[assistants[0]].get("content")
    uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"] \
        if isinstance(content, list) else []
    if len(uses) != 1 or uses[0].get("name") != "Bash" \
            or (uses[0].get("input") or {}).get("command") != command:
        log.info("graph exact command: follow-up not recognised (the assistant turn is not that Bash call)")
        return None
    results = [b for m in later if isinstance(m.get("content"), list) for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"
               and b.get("tool_use_id") == uses[0].get("id")]
    if not results:
        log.info("graph exact command: follow-up not recognised (no tool result for the call)")
        return None
    output = _tool_result_text(results[0]).strip("\n")
    return "report", (exact_output(output) if output.strip() else "(no output)")


def exact_output(output: str, head: int = EXACT_HEAD_CHARS, tail: int = EXACT_TAIL_CHARS) -> str:
    """The first `head` and last `tail` characters of an exact command's output. Measured: the
    graph prints its status markers - CHOSEN, restored, applied subtask N - last, and keeping
    only the first 4,000 characters could cut them."""
    if len(output) <= head + tail:
        return output
    cut = len(output) - head - tail
    return f"{output[:head]}\n[... {cut} chars cut ...]\n{output[-tail:]}"

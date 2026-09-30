#!/usr/bin/env python3
"""Scan files, or the added lines of a unified diff, for known-dangerous code patterns.

The pattern table is adapted from security-guidance/hooks/patterns.py in
anthropics/claude-plugins-official (Apache-2.0). This script is read-only: it opens the
files it is given, prints matches, and does nothing else. No network, no hooks, no state.

Usage:
    python3 scan_patterns.py PATH [PATH ...]      # files or directories
    git diff -U0 | python3 scan_patterns.py --diff  # only lines the diff adds

Output, one line per hit:
    path:line: rule_name: matched text

Exit status: 0 = no hits, 1 = hits found, 2 = usage error.
Each rule_name is explained, with the safe replacement, in ../references/patterns.md.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte")
PY_EXTS = (".py", ".pyi", ".ipynb")
DOC_EXTS = (".md", ".mdx", ".txt", ".rst", ".json", ".yaml", ".yml")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".tox", ".mypy_cache", ".pytest_cache", "target", ".next"}
MAX_BYTES = 2_000_000


def _any(_path: str) -> bool:
    return True


def _js(path: str) -> bool:
    return path.endswith(JS_EXTS)


def _py(path: str) -> bool:
    return path.endswith(PY_EXTS)


def _not_doc(path: str) -> bool:
    return not path.endswith(DOC_EXTS)


def _workflow(path: str) -> bool:
    norm = path.replace("\\", "/")
    return ".github/workflows/" in norm and norm.endswith((".yml", ".yaml"))


# (rule_name, applies_to_path, substrings, regex). A line hits when the path passes the
# gate and the line contains any substring or matches the regex.
RULES: list[tuple[str, object, tuple[str, ...], str | None]] = [
    # Upstream warns on every edit to a workflow file. Here the hit is the line that
    # interpolates an attacker-controllable expression; inside `env:` it is the safe form.
    ("github_actions_workflow", _workflow, (),
     r"\$\{\{\s*github\.(?:event\.|head_ref)"),
    ("child_process_exec", _js, ("child_process.exec", "execSync("),
     r"(?<![a-zA-Z0-9_\.])exec\("),
    ("new_function_injection", _js, ("new Function",), None),
    ("eval_injection", _not_doc, (), r"(?<![a-zA-Z0-9_\.])eval\("),
    ("react_dangerously_set_html", _js, ("dangerouslySetInnerHTML",), None),
    ("document_write_xss", _js, ("document.write",), None),
    ("innerHTML_xss", _js, (".innerHTML =", ".innerHTML="), None),
    ("pickle_deserialization", _py, (),
     r"(?<![a-zA-Z0-9_])pickle\.(loads?|Unpickler)\b|(?<![a-zA-Z0-9_])pkl_load\("),
    ("os_system_injection", _py, ("from os import system",), r"\bos\.system\s*\("),
    ("python_subprocess_shell", _any, (),
     r"subprocess\.(?:run|call|Popen|check_output|check_call)\(.*shell\s*=\s*True"),
    ("go_exec_shell_injection", _any, (), r'exec\.Command\(\s*"(?:sh|bash|/bin/sh|/bin/bash)"'),
    ("unsafe_yaml_load", _any, (), r"\byaml\.load\s*\((?![^)\n]{0,80}\bSafe)"),
    ("node_createcipher_no_iv", _any, (), r"\bcrypto\.(createCipher|createDecipher)\b"),
    ("aes_ecb_mode", _any, (), r"\bAES\.MODE_ECB\b|\bmodes\.ECB\s*\(|[\x22\x27]aes-\d+-ecb[\x22\x27]"),
    ("tls_verification_disabled", _any, (),
     r"\bverify\s*=\s*False\b|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true"
     r"|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*[\x22\x27]?0|ssl\._create_unverified_context"
     r"|check_hostname\s*=\s*False"),
    ("marshal_loads", _any, (), r"\bmarshal\.loads?\s*\("),
    ("shelve_open", _any, (), r"\bshelve\.open\s*\("),
    ("xml_unsafe_parse", _any, (),
     r"\b(xml\.etree\.ElementTree|ElementTree|ET)\.(parse|fromstring|XML)\s*\("
     r"|\bminidom\.(parse|parseString)\s*\(|\bxml\.sax\.(parse|make_parser)\b"),
    ("pickle_variants_load", _any, (), r"\b(cPickle|cloudpickle|dill)\.(load|loads)\s*\("),
    ("outerHTML_xss", _js, (".outerHTML =", ".outerHTML="), None),
    ("insertAdjacentHTML_xss", _js, (".insertAdjacentHTML(",), None),
    ("script_src_without_sri", _any, (),
     r"<script\s+(?![^>]{0,400}integrity\s*=)[^>]{0,200}src\s*=\s*[\x22\x27](?:https?:)?//"
     r"[^\x22\x27]{1,300}[\x22\x27][^>]{0,100}>"),
    ("torch_unsafe_load", _any, (),
     r"(?:\btorch\.load|\.torch_load)\s*\((?![^)\n]{0,200}weights_only\s*=\s*True)"),
    ("yaml_unsafe_load_variants", _any, (), r"(?:\byaml\.unsafe_load|\.yaml_unsafe_load)\s*\("),
    ("pickle_wrapper_load", _any, (),
     r"\bjoblib\.load\s*\(|\b(?:pd|pandas)\.read_pickle\s*\(|\.cloudpickle_load\s*\("
     r"|\b(?:np|numpy)\.load\s*\([^)\n]{0,200}allow_pickle\s*=\s*True"),
]

COMPILED = [(name, gate, subs, re.compile(rx) if rx else None) for name, gate, subs, rx in RULES]


def check_line(path: str, text: str) -> list[tuple[str, str]]:
    """(rule_name, matched text) for every rule this line of `path` hits."""
    hits = []
    for name, gate, subs, rx in COMPILED:
        if not gate(path):
            continue
        found = next((s for s in subs if s in text), None)
        if found is None and rx is not None:
            m = rx.search(text)
            found = m.group(0) if m else None
        if found is not None:
            hits.append((name, found))
    return hits


def _files(paths: list[str]):
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, names in os.walk(path):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for n in sorted(names):
                    yield os.path.join(root, n)
        else:
            yield path


def _read_text(path: str) -> list[str] | None:
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as err:
        print(f"{path}: cannot read: {err}", file=sys.stderr)
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def scan_files(paths: list[str]):
    for path in _files(paths):
        lines = _read_text(path)
        for number, text in enumerate(lines or [], start=1):
            for name, found in check_line(path, text):
                yield path, number, name, found


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_diff(stream):
    """Hits on added lines only, with their line numbers in the new file."""
    path, line = None, 0
    for raw in stream:
        text = raw.rstrip("\n")
        if text.startswith("+++ "):
            target = text[4:].split("\t")[0]
            path = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            continue
        if text.startswith("--- "):
            continue
        hunk = _HUNK.match(text)
        if hunk:
            line = int(hunk.group(1))
            continue
        if path is None:
            continue
        if text.startswith("+"):
            for name, found in check_line(path, text[1:]):
                yield path, line, name, found
            line += 1
        elif text.startswith(" "):
            line += 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="*", help="files or directories to scan")
    parser.add_argument("--diff", action="store_true",
                        help="read a unified diff on stdin and scan only added lines")
    args = parser.parse_args(argv)
    if not args.diff and not args.paths:
        parser.print_usage(sys.stderr)
        return 2

    hits = scan_diff(sys.stdin) if args.diff else scan_files(args.paths)
    count = 0
    for path, number, name, found in hits:
        count += 1
        print(f"{path}:{number}: {name}: {found.strip()[:120]}")
    print(f"{count} hit(s). Look each rule up in references/patterns.md.", file=sys.stderr)
    return 1 if count else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

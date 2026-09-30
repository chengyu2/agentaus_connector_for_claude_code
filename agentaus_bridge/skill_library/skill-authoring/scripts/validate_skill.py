#!/usr/bin/env python3
"""Validate Agent Skill directories against the spec and against what Agentaus's bridge needs.

Standard library only (no PyYAML), so it runs on a bare python3.
Adapted from skill-creator's scripts/quick_validate.py (Apache-2.0), with checks added for
the bridge: the "Use when" routing clause, body size limits, and links to bundled files.

Usage:
    python3 validate_skill.py SKILL_DIR [SKILL_DIR ...]

Prints PASS / WARN / FAIL lines per skill and the routing line Agentaus would see.
Exit status is 1 if any skill has a FAIL, else 0.
"""

from __future__ import annotations

import os
import re
import sys

ALLOWED_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
# Same pattern the bridge uses to find the trigger clause (agentaus_bridge/augment.py).
USE_WHEN_RE = re.compile(
    r"(?:\bUse (?:this (?:skill|agent) |it )?(?:when(?:ever)?|if)\b|\bTriggers on\b|[—;]\s*when\b)"
    r"[:,]?\s*(.+)", re.I)
LINK_RE = re.compile(r"(?<![\w/.-])((?:references|scripts|assets)/[A-Za-z0-9_.\-/]+)")
BLOCK_SCALARS = {">", "|", ">-", "|-", ">+", "|+"}
YAML_INDICATORS = tuple("[]{}*&!|>%@`#,?\"'")
ROUTE_CHARS = 230        # the bridge cuts the routing clause here
INJECT_CHARS = 9000      # project skills injected into the prompt are cut here
LIBRARY_CHARS = 40000    # bridge library skills are cut here
MAX_LINES = 500


def parse_front(block: str) -> tuple[dict, list[str]]:
    """Top-level keys -> {"value": str, "children": [indented lines]}; plus unparsable lines."""
    fields: dict[str, dict] = {}
    bad: list[str] = []
    current = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if current is not None:
                fields[current]["children"].append(line)
            else:
                bad.append(line)
            continue
        key, sep, value = line.partition(":")
        if not sep:
            bad.append(line)
            current = None
            continue
        current = key.strip()
        fields[current] = {"value": value.strip(), "children": []}
    return fields, bad


def scalar(entry: dict) -> tuple[str, str]:
    """(text, style) of a field: style is 'plain', 'quoted' or 'block'."""
    value = entry["value"]
    if value in BLOCK_SCALARS:
        return " ".join(c.strip() for c in entry["children"]), "block"
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1], "quoted"
    return value, "plain"


def plain_scalar_problems(text: str) -> list[str]:
    problems = []
    if ": " in text:
        problems.append("contains ': ' (colon + space) - breaks YAML unless the value is quoted")
    if " #" in text:
        problems.append("contains ' #' - YAML reads the rest as a comment unless quoted")
    if text.startswith(YAML_INDICATORS):
        problems.append(f"starts with {text[0]!r} - a YAML indicator; quote the value")
    return problems


def routing_clause(description: str) -> tuple[str | None, bool]:
    """(clause, was_cut) as the bridge would build it, or (None, False) with no trigger."""
    text = " ".join(description.split())
    found = USE_WHEN_RE.search(text)
    if not found:
        return None, False
    clause = found.group(1)
    end = re.search(r"\.(?:\s|$)", clause)
    clause = (clause[: end.start()] if end else clause).strip().rstrip(".")
    if len(clause) > ROUTE_CHARS:
        return clause[:ROUTE_CHARS].rsplit(" ", 1)[0] + " …", True
    return clause, False


def validate(skill_dir: str) -> int:
    skill_dir = os.path.abspath(skill_dir)
    fails = 0
    print(f"== {skill_dir}")

    def report(level: str, message: str) -> None:
        nonlocal fails
        if level == "FAIL":
            fails += 1
        print(f"  {level}: {message}")

    path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(path):
        report("FAIL", "SKILL.md not found")
        return fails
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    front = FRONT_RE.match(text)
    if not front:
        report("FAIL", "no front matter: the file must start with '---', fields, '---'")
        return fails
    fields, bad = parse_front(front.group(1))
    body = text[front.end():].strip()
    for line in bad:
        report("FAIL", f"front matter line not understood: {line!r}")

    unknown = set(fields) - ALLOWED_KEYS
    if unknown:
        report("WARN", f"keys outside the spec: {', '.join(sorted(unknown))} "
                       f"(allowed: {', '.join(sorted(ALLOWED_KEYS))})")

    # name
    name = scalar(fields["name"])[0].strip() if "name" in fields else ""
    if not name:
        report("FAIL", "missing 'name'")
    else:
        if not NAME_RE.match(name):
            report("FAIL", f"name {name!r}: use a-z, 0-9 and single hyphens, no leading/trailing hyphen")
        if len(name) > 64:
            report("FAIL", f"name is {len(name)} characters; the maximum is 64")
        if name != os.path.basename(skill_dir):
            report("FAIL", f"name {name!r} does not match the directory {os.path.basename(skill_dir)!r}")

    # description
    description, style = scalar(fields["description"]) if "description" in fields else ("", "plain")
    description = " ".join(description.split())
    if not description:
        report("FAIL", "missing 'description'")
    else:
        if len(description) > 1024:
            report("FAIL", f"description is {len(description)} characters; the maximum is 1024")
        if "<" in description or ">" in description:
            report("FAIL", "description contains '<' or '>' (rejected by the upstream validator)")
        if style == "plain":
            for problem in plain_scalar_problems(description):
                report("FAIL", f"description {problem}")
        count = len(re.findall(r"\bUse when\b", description))
        if count == 0:
            report("WARN", "description has no 'Use when' sentence; Agentaus's routing line "
                           "falls back to a vague 'the task needs: ...'")
        elif count > 1:
            report("WARN", "description says 'Use when' more than once; only the first is used")
        clause, cut = routing_clause(description)
        if clause is not None:
            if cut:
                report("WARN", f"routing clause is over {ROUTE_CHARS} characters and will be cut")
            print(f"  routing: If {clause} → `Skill` with `skill: \"{name}\"`")
        print(f"  description: {len(description)} characters")

    # compatibility
    if "compatibility" in fields:
        compat = scalar(fields["compatibility"])[0]
        if len(compat) > 500:
            report("FAIL", f"compatibility is {len(compat)} characters; the maximum is 500")

    # metadata: a map of string to string
    if "metadata" in fields:
        entry = fields["metadata"]
        if entry["value"]:
            report("FAIL", "metadata must be a map (indented 'key: value' lines), not a value")
        for child in entry["children"]:
            key, sep, value = child.strip().partition(":")
            value = value.strip()
            if not sep or not key.strip():
                report("FAIL", f"metadata line not understood: {child.strip()!r}")
                continue
            if value.startswith(("\"", "'")):
                continue
            if re.fullmatch(r"(?i)(true|false|yes|no|null|~|[-+]?\d+(\.\d+)?)", value):
                report("WARN", f"metadata {key.strip()}: {value!r} is not a string in YAML; quote it")
            for problem in plain_scalar_problems(value):
                report("FAIL", f"metadata {key.strip()} {problem}")

    # body
    if not body:
        report("FAIL", "SKILL.md has no body")
    else:
        lines = body.count("\n") + 1
        chars = len(body)
        print(f"  body: {lines} lines, {chars} characters")
        if lines > MAX_LINES:
            report("WARN", f"body is {lines} lines; keep it under {MAX_LINES} and move detail to references/")
        library = os.path.basename(os.path.dirname(skill_dir)) == "skill_library"
        if chars > LIBRARY_CHARS:
            report("WARN", f"body over {LIBRARY_CHARS} characters is cut even as a bridge library skill")
        elif chars > INJECT_CHARS and not library:
            report("WARN", f"body over {INJECT_CHARS} characters is cut if the bridge injects it as a "
                           f"project skill (a bridge library skill may run to {LIBRARY_CHARS})")

    # links to bundled files
    for link in sorted(set(LINK_RE.findall(body))):
        link = link.rstrip(".,;:)")
        if not os.path.exists(os.path.join(skill_dir, link)):
            report("FAIL", f"body mentions {link} but it does not exist")

    # bundled directories: one level deep
    for sub in ("references", "scripts", "assets"):
        folder = os.path.join(skill_dir, sub)
        if os.path.isdir(folder):
            nested = [e for e in os.listdir(folder) if os.path.isdir(os.path.join(folder, e))
                      and e != "__pycache__"]
            if nested:
                report("WARN", f"{sub}/ has subdirectories ({', '.join(nested)}); keep it one level deep")

    print("  PASS" if not fails else f"  {fails} FAIL(s)")
    return fails


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    total = sum(validate(d) for d in argv)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

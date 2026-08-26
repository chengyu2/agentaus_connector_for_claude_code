"""Exact declarations from source code, via Tree-sitter.

The outline exists so search can aim before it reads, and for code that means indexing
declarations. A pattern over lines gets most of them and is wrong in ways that matter: it
cannot tell a declaration from the same words inside a string or a comment, it misses
anything written across two lines, and every language needs its own special case.

Tree-sitter parses properly. It produces a Concrete Syntax Tree - every token preserved,
each mapped back to an exact byte, line and column - so a declaration is identified by
what the grammar says it is rather than by what the line looks like.

There is a strict quality ordering here rather than competing implementations:

    1. Tree-sitter        exact, 19 languages, needs the optional dependency
    2. Python `ast`       exact, one language, always available
    3. declaration pass   approximate, any language, always available

Each rung runs only when the one above is unavailable. That is a fallback chain, not two
answers to the same question.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("agentaus-bridge")

# Suffix -> grammar name in tree-sitter-language-pack.
LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".scala": "scala",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".sql": "sql", ".lua": "lua", ".dart": "dart",
}

# Node types that declare something worth putting in an outline, and the field holding
# the name. Keyed by grammar because every language names its own constructs.
_DECLARATIONS = {
    # `assignment` is here for module-level constants. A configuration constant is
    # exactly the kind of thing someone searches for by name, and losing it would make
    # this outline less useful than the `ast` pass it replaces.
    "python": {"function_definition": "name", "class_definition": "name",
               "decorated_definition": None, "assignment": "left"},
    "javascript": {"function_declaration": "name", "class_declaration": "name",
                   "method_definition": "name", "generator_function_declaration": "name"},
    "typescript": {"function_declaration": "name", "class_declaration": "name",
                   "method_definition": "name", "interface_declaration": "name",
                   "type_alias_declaration": "name", "enum_declaration": "name",
                   "abstract_class_declaration": "name"},
    "tsx": {"function_declaration": "name", "class_declaration": "name",
            "method_definition": "name", "interface_declaration": "name",
            "type_alias_declaration": "name", "enum_declaration": "name"},
    "go": {"function_declaration": "name", "method_declaration": "name",
           "type_declaration": None},
    "rust": {"function_item": "name", "struct_item": "name", "enum_item": "name",
             "trait_item": "name", "impl_item": None, "mod_item": "name",
             "type_item": "name"},
    "java": {"class_declaration": "name", "interface_declaration": "name",
             "method_declaration": "name", "enum_declaration": "name",
             "record_declaration": "name"},
    "kotlin": {"class_declaration": "name", "function_declaration": "name",
               "object_declaration": "name"},
    "swift": {"class_declaration": "name", "function_declaration": "name",
              "protocol_declaration": "name"},
    "c": {"function_definition": None, "struct_specifier": "name",
          "enum_specifier": "name", "type_definition": None},
    "cpp": {"function_definition": None, "class_specifier": "name",
            "struct_specifier": "name", "namespace_definition": "name"},
    "csharp": {"class_declaration": "name", "interface_declaration": "name",
               "method_declaration": "name", "struct_declaration": "name",
               "record_declaration": "name", "enum_declaration": "name"},
    "ruby": {"method": "name", "class": "name", "module": "name",
             "singleton_method": "name"},
    "php": {"function_definition": "name", "class_declaration": "name",
            "method_declaration": "name", "interface_declaration": "name",
            "trait_declaration": "name"},
    "scala": {"function_definition": "name", "class_definition": "name",
              "object_definition": "name", "trait_definition": "name"},
    "bash": {"function_definition": "name"},
    "sql": {"create_table": None, "create_view": None, "create_function": None},
    "lua": {"function_declaration": "name"},
    "dart": {"class_definition": "name", "function_signature": "name",
             "method_signature": "name"},
}

_parsers: dict = {}
_unavailable = False


def available() -> bool:
    """Whether Tree-sitter and its grammars are installed."""
    global _unavailable
    if _unavailable:
        return False
    try:
        import tree_sitter_language_pack  # noqa: F401
        return True
    except ImportError:
        _unavailable = True
        return False


def language_for(path: str) -> str | None:
    return LANGUAGES.get(os.path.splitext(path)[1].lower())


def _parser(language: str):
    if language not in _parsers:
        from tree_sitter_language_pack import get_parser
        _parsers[language] = get_parser(language)
    return _parsers[language]


def _name_of(node: Any, field: str | None, source: bytes) -> str:
    """The declared name, from the grammar's own field where there is one."""
    if field:
        child = node.child_by_field_name(field)
        if child is not None:
            return source[child.start_byte:child.end_byte].decode("utf-8", "replace")
    # No named field. A C function's name is inside its declarator; a Rust `impl` has a
    # type rather than a name; a Go `type_declaration` wraps a `type_spec` that holds the
    # identifier. So look a little deeper rather than only at direct children.
    def first_identifier(current: Any, depth: int = 0) -> str:
        if depth > 2:
            return ""
        for child in current.children:
            if "identifier" in child.type or child.type in (
                "type_identifier", "declarator", "function_declarator"
            ):
                text = source[child.start_byte:child.end_byte].decode("utf-8", "replace")
                return text.split("(")[0].strip()
        for child in current.children:
            found = first_identifier(child, depth + 1)
            if found:
                return found
        return ""

    return first_identifier(node)


def _signature(node: Any, source: bytes, limit: int = 90) -> str:
    """The declaration's own first line, which is its signature."""
    text = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
    first = text.split("\n", 1)[0].strip().rstrip("{:").strip()
    return first[:limit] + ("…" if len(first) > limit else "")


def outline_of(path: str, body: str) -> list[tuple[int, int, str]]:
    """(line, depth, signature) for every declaration Tree-sitter finds.

    Returns [] when Tree-sitter cannot handle the file, so the caller falls through to the
    next rung of the chain rather than treating an empty outline as "no declarations".
    """
    language = language_for(path)
    if not language or not available():
        return []
    wanted = _DECLARATIONS.get(language)
    if not wanted:
        return []

    source = body.encode("utf-8", "replace")
    try:
        tree = _parser(language).parse(source)
    except Exception as exc:
        log.warning("tree-sitter could not parse %s (%s)", path, exc)
        return []

    found: list[tuple[int, int, str]] = []

    def walk(node: Any, depth: int) -> None:
        for child in node.children:
            if child.type in wanted:
                # A decorated definition wraps the real one; descend rather than name it.
                if child.type == "decorated_definition":
                    walk(child, depth)
                    continue
                name = _name_of(child, wanted[child.type], source)
                # Only SHOUTING module-level names: every local assignment would drown
                # the outline it is meant to be.
                if child.type == "assignment" and not (
                    depth == 1 and name and name.isupper()
                ):
                    continue
                if name:
                    label = (f"{name} (constant)" if child.type == "assignment"
                             else _signature(child, source))
                    found.append((child.start_point[0] + 1, depth, label))
                walk(child, min(depth + 1, 4))
            else:
                walk(child, depth)

    walk(tree.root_node, 1)
    found.sort()
    return found

"""A free structural map, so search can aim before it reads.

Search finds things by reading: one model call per chunk. That is what makes it work when
the answer shares no words with the question, and it is also what makes it cost ten calls
on a large file. The cheap half of the problem is structural - "which part of this is about
X" follows from declarations or headings - and structure costs nothing to extract.

Structure means different things in different files, so there are three extractors. That
is not the same mistake as having two extractors for one structure: a function signature
and a heading are not competing descriptions of the same thing.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import outline  # noqa: E402


def reader(files):
    return lambda path: files.get(path, "")


class TestCodeIsIndexedByDeclaration(unittest.TestCase):
    PY_SRC = '''
"""A module."""
import os

MAX_RETRIES = 3

def top_level(a, b):
    def nested_helper():
        return 1
    return nested_helper()

class Thing:
    def method(self, x):
        pass

    async def async_method(self):
        pass
'''

    def test_python_uses_the_real_parser(self):
        """Signatures come back richer from Tree-sitter than from a pattern, so these
        assert what the entry names rather than its exact text."""
        got = outline.of_file("/x/m.py", reader({"/x/m.py": self.PY_SRC}))
        titles = " | ".join(t for _l, _d, t in got)
        self.assertIn("def top_level(a, b)", titles)
        self.assertIn("class Thing", titles)
        self.assertIn("async def async_method", titles)
        self.assertIn("MAX_RETRIES (constant)", titles)

    def test_a_local_assignment_does_not_drown_the_outline(self):
        src = "TOP = 1\ndef f():\n    local_thing = 2\n    OTHER = 3\n"
        titles = " | ".join(t for _l, _d, t in
                            outline.of_file("/x/m.py", reader({"/x/m.py": src})))
        self.assertIn("TOP (constant)", titles)
        self.assertNotIn("local_thing", titles)
        self.assertNotIn("OTHER", titles)

    def test_line_numbers_are_addressable(self):
        got = outline.of_file("/x/m.py", reader({"/x/m.py": self.PY_SRC}))
        line = next(l for l, _d, t in got if t.startswith("class Thing"))
        self.assertEqual(self.PY_SRC.splitlines()[line - 1].strip(), "class Thing:")

    def test_a_string_containing_def_is_not_a_definition(self):
        """What an exact parser gives that a pattern would not."""
        src = 'DOC = """\ndef not_a_function():\n    pass\n"""\n'
        titles = [t for _l, _d, t in outline.of_file("/x/m.py", reader({"/x/m.py": src}))]
        self.assertNotIn("def not_a_function()", titles)

    def test_other_languages_get_a_declaration_pass(self):
        go = "package main\n\nfunc Handle(w, r) {\n}\n\ntype Server struct {\n}\n"
        titles = " | ".join(t for _l, _d, t in
                            outline.of_file("/x/m.go", reader({"/x/m.go": go})))
        self.assertIn("func Handle", titles)
        self.assertIn("type Server", titles)

    def test_typescript_and_rust_too(self):
        ts = "export function render(props) {}\nexport class View {}\n"
        rs = "pub fn main() {}\npub struct Config {}\nimpl Config {}\n"
        ts_titles = " | ".join(t for _l, _d, t in
                               outline.of_file("/x/a.ts", reader({"/x/a.ts": ts})))
        rs_titles = " | ".join(t for _l, _d, t in
                               outline.of_file("/x/a.rs", reader({"/x/a.rs": rs})))
        self.assertIn("function render", ts_titles)
        self.assertIn("class View", ts_titles)
        self.assertIn("fn main", rs_titles)
        self.assertIn("struct Config", rs_titles)

    def test_unparseable_python_falls_back_rather_than_returning_nothing(self):
        broken = "# heading-ish\ndef ( this is not python\n## A Heading\n"
        got = outline.of_file("/x/m.py", reader({"/x/m.py": broken}))
        self.assertTrue(got, "a file that will not parse produced no outline at all")


class TestProseIsIndexedByHeading(unittest.TestCase):
    def test_markdown_headings_with_depth(self):
        md = "# Top\nprose\n## Second\nmore\n### Third\n"
        got = outline.of_file("/x/a.md", reader({"/x/a.md": md}))
        self.assertEqual([(d, t) for _l, d, t in got],
                         [(1, "Top"), (2, "Second"), (3, "Third")])

    def test_bold_lines_and_numbered_clauses_count(self):
        body = "**Security certifications**\ntext\n5.3.1 Adherence to standards\n"
        titles = [t for _l, _d, t in outline.of_file("/x/a.md", reader({"/x/a.md": body}))]
        self.assertIn("Security certifications", titles)
        self.assertTrue(any(t.startswith("5.3.1") for t in titles))

    def test_a_long_line_is_prose_not_a_heading(self):
        body = "**" + "x" * 400 + "**\n"
        self.assertEqual(outline.of_file("/x/a.md", reader({"/x/a.md": body})), [])


class TestJsonIsIndexedByKey(unittest.TestCase):
    def test_object_keys(self):
        got = outline.of_file("/x/a.json", reader({"/x/a.json": '{"answers": 1, "meta": 2}'}))
        titles = [t for _l, _d, t in got]
        self.assertIn("key: answers", titles)
        self.assertIn("key: meta", titles)

    def test_a_list_of_records_reports_its_fields(self):
        got = outline.of_file("/x/a.json",
                              reader({"/x/a.json": '[{"ref": 1, "answer": 2}]'}))
        self.assertIn("fields: ref, answer", got[0][2])

    def test_malformed_json_yields_nothing_rather_than_raising(self):
        self.assertEqual(outline.of_file("/x/a.json", reader({"/x/a.json": "{oops"})), [])


class TestRenderingAndPicking(unittest.TestCase):
    def test_render_is_tagged_and_carries_line_numbers(self):
        files = {"/x/a.py": "def one():\n    pass\n"}
        text = outline.render(["/x/a.py"], read=reader(files))
        self.assertIn('<file path="/x/a.py">', text)
        self.assertIn('<section line="1"', text)
        self.assertIn("def one()", text)

    def test_picks_are_parsed(self):
        picks = outline.read_picks("/x/a.py:12\n/x/b.md:340", ["/x/a.py", "/x/b.md"])
        self.assertEqual(picks, [("/x/a.py", 12), ("/x/b.md", 340)])

    def test_an_invented_path_is_dropped(self):
        """Same reason an invented tool name is caught rather than followed."""
        picks = outline.read_picks("/x/real.py:1\n/x/imaginary.py:9", ["/x/real.py"])
        self.assertEqual(picks, [("/x/real.py", 1)])

    def test_none_means_the_outline_told_it_nothing(self):
        self.assertEqual(outline.read_picks("NONE", ["/x/a.py"]), [])

    def test_garbage_yields_no_picks_so_search_reads_everything(self):
        """Aiming is an optimisation; missing the answer is not its price."""
        self.assertEqual(outline.read_picks("I'm not sure, maybe look around?", ["/x/a.py"]), [])

    def test_duplicate_picks_collapse(self):
        picks = outline.read_picks("/x/a.py:5\n/x/a.py:5", ["/x/a.py"])
        self.assertEqual(picks, [("/x/a.py", 5)])


class TestItActuallyShrinksThings(unittest.TestCase):
    def test_a_code_tree_outlines_far_smaller_than_it_reads(self):
        from agentaus_bridge import tools
        from agentaus_bridge.tokens import count_tokens
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "agentaus_bridge")
        files = sorted(tools.enumerate_files(root))
        self.assertGreater(len(files), 5)
        content = sum(count_tokens(tools.read_text(f)) for f in files)
        structure = count_tokens(outline.render(files, read=tools.read_text))
        self.assertGreater(content / max(1, structure), 4.0,
                           f"outline is {structure} tokens against {content} of content")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTreeSitterIsExactWhereAPatternIsNot(unittest.TestCase):
    """The reason for the dependency: a pattern cannot tell a declaration from the same
    words inside a string or a comment, and misses anything written across two lines."""

    def setUp(self):
        from agentaus_bridge import symbols
        self.symbols = symbols
        if not symbols.available():
            self.skipTest("tree-sitter is not installed here")

    def test_a_declaration_inside_a_docstring_is_not_a_declaration(self):
        src = 'DOC = """\ndef fake_function():\n    pass\n"""\n\ndef real_one():\n    pass\n'
        titles = " | ".join(t for _l, _d, t in self.symbols.outline_of("/x/a.py", src))
        self.assertIn("def real_one", titles)
        self.assertNotIn("fake_function", titles)

    def test_a_declaration_inside_a_comment_is_not_a_declaration(self):
        src = "// func Commented(x int) {}\nfunc Real(y int) {}\n"
        titles = " | ".join(t for _l, _d, t in self.symbols.outline_of("/x/a.go", src))
        self.assertIn("func Real", titles)
        self.assertNotIn("Commented", titles)

    def test_a_signature_split_across_lines_is_still_found(self):
        src = "func Handle(\n    w http.ResponseWriter,\n    r *http.Request,\n) {\n}\n"
        titles = " | ".join(t for _l, _d, t in self.symbols.outline_of("/x/a.go", src))
        self.assertIn("func Handle", titles)

    def test_nineteen_grammars_are_reachable(self):
        reachable = sum(1 for lang in set(self.symbols.LANGUAGES.values())
                        if self.symbols.outline_of(
                            f"/x/a{next(k for k, v in self.symbols.LANGUAGES.items() if v == lang)}",
                            "") == [])
        self.assertGreaterEqual(len(set(self.symbols.LANGUAGES.values())), 15)

    def test_an_unknown_extension_returns_nothing_so_the_caller_falls_through(self):
        self.assertEqual(self.symbols.outline_of("/x/a.unknownext", "def x(): pass"), [])

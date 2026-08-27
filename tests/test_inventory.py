"""What is in a folder, answered by looking rather than by searching.

The gap this fills, measured: asked what kinds of documents were in a 312-file folder,
two semantic searches took 183s and 52s and returned 116 and 130 characters between
them, and the turn timed out having learned nothing. Search asks each excerpt whether it
answers the question; no single excerpt answers a question about the whole corpus, so
every one says NONE.

That is not the model being weak. It had no instrument for the question it was asked.
"""

import asyncio
import os
import tempfile
import unittest

from agentaus_bridge import inventory, tools


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Tree:
    def __init__(self, files):
        self.files = files

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = self.dir.name
        for name, body in self.files.items():
            full = os.path.join(self.path, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(body)
        return self

    def __exit__(self, *exc):
        self.dir.cleanup()


class WhatItReports(unittest.TestCase):
    def test_counts_every_kind_of_file(self):
        with Tree({"a.md": "# One\n", "b.md": "# Two\n", "c.py": "def f():\n    pass\n",
                   "sub/d.json": '{"k": 1}\n'}) as tree:
            out = inventory.render(tree.path, tools.enumerate_files(tree.path),
                                   read=tools.read_text)
        self.assertIn('name="md" count="2"', out)
        self.assertIn('name="py" count="1"', out)
        self.assertIn('files="4"', out)

    def test_names_real_files_so_an_answer_can_cite_them(self):
        with Tree({"report.md": "# Quarterly report\n", "notes.md": "# Notes\n"}) as tree:
            out = inventory.render(tree.path, tools.enumerate_files(tree.path),
                                   read=tools.read_text)
        self.assertIn("report.md", out)
        self.assertIn("notes.md", out)

    def test_says_what_a_file_is_from_its_own_structure(self):
        """A headline taken from the document, never guessed from the filename."""
        with Tree({"x.md": "# Tender response for Inner West Council\n\nbody\n"}) as tree:
            out = inventory.render(tree.path, tools.enumerate_files(tree.path),
                                   read=tools.read_text)
        self.assertIn("Tender response for Inner West", out)

    def test_folders_are_reported_separately(self):
        with Tree({"one/a.md": "# A\n", "two/b.md": "# B\n"}) as tree:
            out = inventory.render(tree.path, tools.enumerate_files(tree.path),
                                   read=tools.read_text)
        self.assertIn('path="one"', out)
        self.assertIn('path="two"', out)

    def test_an_empty_folder_says_so_rather_than_failing(self):
        with Tree({}) as tree:
            out = inventory.render(tree.path, [], read=tools.read_text)
        self.assertIn("no readable files", out)


class TheDetailLimit(unittest.TestCase):
    def test_a_large_tree_summarises_and_says_that_it_did(self):
        """A summary that looks like a listing is how a partial survey is reported
        as a complete one."""
        files = {f"f{n}.md": f"# Doc {n}\n" for n in range(inventory.DETAIL_LIMIT + 10)}
        with Tree(files) as tree:
            out = inventory.render(tree.path, tools.enumerate_files(tree.path),
                                   read=tools.read_text)
        self.assertIn("detail limit", out)
        self.assertIn("narrower path", out)

    def test_a_small_tree_lists_every_file(self):
        files = {f"f{n}.md": f"# Doc {n}\n" for n in range(5)}
        with Tree(files) as tree:
            out = inventory.render(tree.path, tools.enumerate_files(tree.path),
                                   read=tools.read_text)
        for n in range(5):
            self.assertIn(f"f{n}.md", out)


class AsATool(unittest.TestCase):
    def test_it_runs_with_no_model_calls_at_all(self):
        calls = []

        async def never(text):
            calls.append(text)
            return ""

        with Tree({"a.md": "# A\n", "b.py": "def f():\n    pass\n"}) as tree:
            out = run(tools.execute("agentaus_inventory", {"path": tree.path}, never))
        self.assertIn("<inventory", out)
        self.assertEqual(calls, [], "an inventory must not cost a model call")

    def test_a_relative_path_is_resolved_against_the_working_directory(self):
        with Tree({"a.md": "# A\n"}) as tree:
            out = run(tools.execute("agentaus_inventory", {"path": "."},
                                    lambda t: None, tree.path))
        self.assertIn("<inventory", out)

    def test_a_missing_path_is_reported_not_raised(self):
        out = run(tools.execute("agentaus_inventory",
                                {"path": "/nonexistent-xyz"}, lambda t: None))
        self.assertIn("No such path", out)

    def test_a_glob_narrows_it(self):
        with Tree({"a.md": "# A\n", "b.py": "x = 1\n"}) as tree:
            out = run(tools.execute("agentaus_inventory",
                                    {"path": tree.path, "glob": "*.md"}, lambda t: None))
        self.assertIn("a.md", out)
        self.assertNotIn("b.py", out)

    def test_it_is_a_bridge_tool_and_never_reaches_the_client(self):
        self.assertIn(tools.INVENTORY_TOOL, tools.BRIDGE_TOOLS)

    def test_secrets_are_excluded_from_a_listing_too(self):
        """An inventory names files. It must not name the ones that must never be read."""
        with Tree({"a.md": "# A\n", ".env": "KEY=x\n",
                   "credentials.json": "{}\n"}) as tree:
            out = run(tools.execute("agentaus_inventory", {"path": tree.path},
                                    lambda t: None))
        self.assertIn("a.md", out)
        self.assertNotIn(".env", out)
        self.assertNotIn("credentials.json", out)


if __name__ == "__main__":
    unittest.main()

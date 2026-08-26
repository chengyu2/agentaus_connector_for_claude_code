"""Does the code in an answer parse?

`ast.parse` decides it instantly and is never a matter of opinion. The self-review pass is
asked something else - "is this correct" - which genuinely needs a model, and it passed an
answer whose bracket did not match: HumanEval/100 failed with "closing parenthesis ']'
does not match opening" on a turn where review ran and revised nothing. Claude Opus solved
the same problem from the same prompt, so that gap was a typo rather than a
misunderstanding.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import syntax  # noqa: E402


class TestFindingCode(unittest.TestCase):
    def test_a_fenced_python_block(self):
        self.assertEqual(syntax.blocks("text\n```python\nx = 1\n```"), ["x = 1\n"])

    def test_an_unlabelled_fence_counts_as_python(self):
        """In a coding answer an unlabelled block is Python far more often than not."""
        self.assertEqual(syntax.blocks("```\nx = 1\n```"), ["x = 1\n"])

    def test_another_language_is_left_alone(self):
        self.assertEqual(syntax.blocks("```rust\nfn main() {}\n```"), [])
        self.assertEqual(syntax.first_error("```rust\nfn main() { let x = ; }\n```"), "")

    def test_bare_code_is_recognised(self):
        self.assertEqual(syntax.blocks("def f():\n    return 1\n"), ["def f():\n    return 1"])

    def test_prose_is_not_mistaken_for_code(self):
        """Otherwise an explanation gets sent away to be 'fixed'."""
        for text in ("The cap is enforced in gate.py: it uses a semaphore.",
                     "Two things matter here: precision, and recall.",
                     "I could not find that in the repository."):
            self.assertEqual(syntax.blocks(text), [], text[:40])


class TestDetection(unittest.TestCase):
    def test_the_bracket_slip_that_started_this(self):
        error = syntax.first_error("def make_a_pile(n):\n    return [n + 2 * i for i in range(n)}\n")
        self.assertIn("closing parenthesis", error)
        self.assertIn("line 2", error)

    def test_valid_code_reports_nothing(self):
        self.assertEqual(syntax.first_error("def f():\n    return [1, 2]\n"), "")

    def test_an_answer_with_no_code_reports_nothing(self):
        for text in ("", None, "It is handled in gate.py."):
            self.assertEqual(syntax.first_error(text), "", repr(text))

    def test_the_first_broken_block_of_several_is_reported(self):
        text = "```python\nx = 1\n```\nand\n```python\ndef f(:\n```"
        self.assertTrue(syntax.first_error(text))

    def test_an_indentation_error_counts(self):
        self.assertTrue(syntax.first_error("def f():\nreturn 1\n"))

    def test_the_fix_prompt_carries_the_error_and_says_not_to_redesign(self):
        prompt = syntax.FIX_INSTRUCTION.format(error="bad bracket", answer="def f(:")
        self.assertIn("bad bracket", prompt)
        self.assertIn("do not change the", prompt.lower())


class TestTheRepairPath(unittest.IsolatedAsyncioTestCase):
    async def test_a_fix_that_parses_is_accepted(self):
        from unittest import mock

        from agentaus_bridge import server

        async def fixer(_client, _text):
            return "def f():\n    return [1, 2]\n"

        with mock.patch.object(server, "_agentaus_summarise", fixer):
            out = await server._fix_syntax(None, "def f():\n    return [1, 2}\n")
        self.assertEqual(syntax.first_error(out), "")

    async def test_a_fix_that_still_breaks_is_rejected(self):
        """The original is no worse, and may be closer to right."""
        from unittest import mock

        from agentaus_bridge import server
        original = "def f():\n    return [1, 2}\n"

        async def worse(_client, _text):
            return "def g(:\n"

        with mock.patch.object(server, "_agentaus_summarise", worse):
            out = await server._fix_syntax(None, original)
        self.assertEqual(out, original)

    async def test_a_failed_call_keeps_the_answer(self):
        from unittest import mock

        from agentaus_bridge import server
        original = "def f():\n    return [1, 2}\n"

        async def broken(_client, _text):
            raise RuntimeError("upstream down")

        with mock.patch.object(server, "_agentaus_summarise", broken):
            self.assertEqual(await server._fix_syntax(None, original), original)

    async def test_valid_code_costs_no_call_at_all(self):
        from unittest import mock

        from agentaus_bridge import server
        called = []

        async def counting(_client, _text):
            called.append(1)
            return "x"

        with mock.patch.object(server, "_agentaus_summarise", counting):
            await server._fix_syntax(None, "def f():\n    return 1\n")
        self.assertEqual(called, [], "a model was called on code that already parses")


if __name__ == "__main__":
    unittest.main(verbosity=2)

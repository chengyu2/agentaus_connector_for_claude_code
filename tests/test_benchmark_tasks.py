"""The benchmark's own tasks must be sound.

A task whose assertions cannot be satisfied fails both arms equally and teaches
nothing, while looking like a genuine result. Every task is checked against a known
good solution here so a broken one is caught before it is used to judge anything.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.reference import REFS  # noqa: E402
from benchmarks.run import extract_code, run_tests  # noqa: E402
from benchmarks.tasks import TASKS  # noqa: E402


class TestTasksAreSound(unittest.TestCase):
    def test_every_task_has_a_reference_solution(self):
        missing = [t["id"] for t in TASKS if t["id"] not in REFS]

        self.assertEqual(missing, [], f"no reference solution for: {missing}")

    def test_every_task_is_satisfiable(self):
        failures = []
        for task in TASKS:
            ref = REFS.get(task["id"])
            if ref is None:
                continue
            ok, detail = run_tests(ref, task["tests"], task["entry"])
            if not ok:
                failures.append(f"{task['id']}: {detail}")

        self.assertEqual(failures, [], f"unsatisfiable tasks: {failures}")

    def test_task_ids_are_unique(self):
        ids = [t["id"] for t in TASKS]

        self.assertEqual(len(ids), len(set(ids)), "duplicate task ids")

    def test_every_task_declares_its_entry_point(self):
        for task in TASKS:
            self.assertIn(task["entry"], task["tests"],
                          f"{task['id']}: tests never call {task['entry']}")

    def test_a_wrong_solution_actually_fails(self):
        """If a broken solution still passed, the tests would not be testing anything."""
        broken = "def median(nums):\n    return sorted(nums)[len(nums)//2]\n"
        task = next(t for t in TASKS if t["id"] == "median")

        ok, _ = run_tests(broken, task["tests"], task["entry"])

        self.assertFalse(ok, "a solution that mishandles even-length input passed")


class TestCodeExtraction(unittest.TestCase):
    def test_fenced_code_is_extracted(self):
        answer = "Here you go:\n```python\ndef f():\n    return 1\n```\nHope that helps."

        self.assertIn("def f():", extract_code(answer))
        self.assertNotIn("Hope that helps", extract_code(answer))

    def test_bare_code_passes_through(self):
        self.assertIn("def f():", extract_code("def f():\n    return 1"))

    def test_the_largest_block_wins(self):
        """Models often show a short usage example after the real answer."""
        answer = "```python\nf(1)\n```\nand the function:\n```python\ndef f(x):\n    return x + 1\n```"

        self.assertIn("def f(x):", extract_code(answer))


if __name__ == "__main__":
    unittest.main()

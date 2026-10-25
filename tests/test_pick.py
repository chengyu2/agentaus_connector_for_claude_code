"""The graph's change picker (PICK_PY in graphs/dynamic.js), run as the graph runs it."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPH = os.path.join(ROOT, "agentaus_bridge", "graphs", "dynamic.js")

ORIGINAL = '''import statistics

RANKS = {"a": [1, 2, 9], "b": [3, 4, 5]}


def mean_rank(theme):
    return statistics.median(RANKS[theme])
'''
FIXED = '''def mean_rank(theme):
    return statistics.mean(RANKS[theme])
'''


def pick_source() -> str:
    src = open(GRAPH, encoding="utf-8").read()
    return re.search(r"const PICK_PY = String\.raw`(.*?)`\n", src, re.S).group(1)


class PytestStyleTests(unittest.TestCase):
    """Measured (d16 c1913h): the one test telling the mean from the median was a
    `def test_...():` never called, so run as a script it passed every candidate and the
    unchanged original won the audit."""

    def test_a_test_importing_the_module_by_its_bare_name_runs(self):
        # Measured (d16 c1913): `import bank` for tools/bank.py failed on every candidate.
        chosen = self.pick("\nimport bank\nassert bank.mean_rank('a') == 4\n", bare=True)
        self.assertIn("changed True", chosen)

    def pick(self, test_body: str, bare: bool = False) -> str:
        td = tempfile.mkdtemp(prefix="pick-")
        root = os.path.join(td, "pristine")
        os.makedirs(os.path.join(root, "tools"))
        with open(os.path.join(root, "tools", "bank.py"), "w") as fh:
            fh.write(ORIGINAL)
        solver = os.path.join(td, "s1", "1")
        os.makedirs(os.path.join(solver, "repo", "tools"))
        with open(os.path.join(solver, "repo", "tools", "bank.py"), "w") as fh:
            fh.write(ORIGINAL)
        with open(os.path.join(solver, "mean_rank.py"), "w") as fh:
            fh.write(FIXED)
        tests = os.path.join(solver, "tests.py")
        with open(tests, "w") as fh:
            fh.write(f"import sys\nsys.path.insert(0, {os.path.join(solver, 'repo')!r})\n"
                     + ("" if bare else "from tools.bank import mean_rank\n") + test_body)
        pick = os.path.join(td, "pick.py")
        with open(pick, "w") as fh:
            fh.write(pick_source())
        out = subprocess.run(
            [sys.executable, pick, root, "tools/bank.py", "mean_rank", "-", f"1|{solver}|1", "--", tests],
            env=dict(os.environ, PICK_AUDIT="1"), capture_output=True, text=True, timeout=120).stdout
        return [l for l in out.splitlines() if l.startswith("CHOSEN")][-1]

    def test_a_never_called_test_function_is_run(self):
        chosen = self.pick("\n\ndef test_mean():\n    assert mean_rank('a') == 4\n")
        self.assertIn("changed True", chosen)

    def test_a_file_that_calls_its_tests_is_left_alone(self):
        chosen = self.pick("\n\ndef test_mean():\n    assert mean_rank('a') == 4\n\n\ntest_mean()\n")
        self.assertIn("changed True", chosen)

    def test_a_test_the_original_passes_does_not_beat_it(self):
        chosen = self.pick("\n\ndef test_b():\n    assert mean_rank('b') == 4\n")
        self.assertIn("changed False", chosen)


if __name__ == "__main__":
    unittest.main()

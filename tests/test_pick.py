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

    def test_a_test_that_loads_the_solvers_own_file_tests_the_candidate(self):
        # Measured (d16 c1913h): count_top's test loaded s16/1/count_top.py by path.
        chosen = self.pick("\nimport importlib.util\n"
                           "s = importlib.util.spec_from_file_location('mine', __OWN__)\n"
                           "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                           "assert m.mean_rank('a') == 4\n")
        self.assertIn("changed True", chosen)

    def test_an_exact_copy_of_a_module_table_is_dropped_not_penalised(self):
        # Measured (d16 c1913h): _clean's fix pasted in FIX and lost on the shadow penalty.
        chosen = self.pick("\n\ndef test_mean():\n    assert mean_rank('a') == 4\n",
                           fixed='''def mean_rank(theme):
    RANKS = {"a": [1, 2, 9], "b": [3, 4, 5]}
    return statistics.mean(RANKS[theme])
''')
        self.assertIn("changed True", chosen)
        with open(self.frozen) as fh:
            self.assertNotIn("RANKS = ", fh.read())

    def test_a_function_importing_from_its_own_module_has_that_import_dropped(self):
        # Measured (d16 c1913): count_top's fix began `from tools.bank import rank, FULL`.
        chosen = self.pick("\n\ndef test_mean():\n    assert mean_rank('a') == 4\n",
                           fixed='''def mean_rank(theme):
    from tools.bank import RANKS
    return statistics.mean(RANKS[theme])
''')
        self.assertIn("changed True", chosen)
        with open(self.frozen) as fh:
            self.assertNotIn("import RANKS", fh.read())

    def test_a_change_judged_a_fix_wins_where_nothing_measured_it(self):
        self.assertIn("changed False", self.pick("\n"))
        self.assertIn("changed True", self.pick("\n", prefer="1"))

    def test_a_judged_fix_still_loses_to_a_test_the_original_passes(self):
        self.assertIn("changed False", self.pick("\nassert mean_rank('b') == 4\nassert mean_rank('a') == 2\n", prefer="1"))

    def pick(self, test_body: str, bare: bool = False, fixed: str = FIXED, prefer: str = "") -> str:
        td = tempfile.mkdtemp(prefix="pick-")
        self.frozen = os.path.join(td, "chosen", "mean_rank.py")
        root = os.path.join(td, "pristine")
        os.makedirs(os.path.join(root, "tools"))
        with open(os.path.join(root, "tools", "bank.py"), "w") as fh:
            fh.write(ORIGINAL)
        solver = os.path.join(td, "s1", "1")
        os.makedirs(os.path.join(solver, "repo", "tools"))
        with open(os.path.join(solver, "repo", "tools", "bank.py"), "w") as fh:
            fh.write(ORIGINAL)
        with open(os.path.join(solver, "mean_rank.py"), "w") as fh:
            fh.write(fixed)
        tests = os.path.join(solver, "tests.py")
        with open(tests, "w") as fh:
            fh.write(f"import sys\nsys.path.insert(0, {os.path.join(solver, 'repo')!r})\n"
                     + ("" if bare else "from tools.bank import mean_rank\n")
                     + test_body.replace("__OWN__", repr(os.path.join(solver, "mean_rank.py"))))
        pick = os.path.join(td, "pick.py")
        with open(pick, "w") as fh:
            fh.write(pick_source())
        out = subprocess.run(
            [sys.executable, pick, root, "tools/bank.py", "mean_rank", "-", f"1|{solver}|1", "--", tests],
            env=dict(os.environ, PICK_AUDIT="1", PICK_OUT=self.frozen, PICK_PREFER=prefer), capture_output=True, text=True, timeout=120).stdout
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

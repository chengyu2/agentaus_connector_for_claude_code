"""An Edit that leaves a name undefined in a Python function is said so. See name_check.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import name_check  # noqa: E402

AFTER = '''def scale_points(points: list, factor: float, origin=(0.0, 0.0)):
    ox, oy = origin
    return [(ox + (x - ox) * k, oy + (y - oy) * k) for x, y in points]


def double(points):
    return scale_points(points, factor=2.0)
'''


def body(path, old, new, is_error=False):
    return {"messages": [
        {"role": "user", "content": "rename k to factor"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "e1", "name": "Edit",
                                           "input": {"file_path": path, "old_string": old,
                                                     "new_string": new}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "e1",
                                      "is_error": is_error, "content": "updated"}]},
    ]}


def later(b, path):
    """The conversation after one more successful Edit elsewhere."""
    msgs = list(b["messages"]) + [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "e2", "name": "Edit",
                                           "input": {"file_path": path, "old_string": "factor=3.0",
                                                     "new_string": "factor=2.0"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "e2",
                                      "content": "updated"}]}]
    return {"messages": msgs}


class NameCheck(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "shapes.py")
        with open(self.path, "w") as fh:
            fh.write(AFTER)

    def tearDown(self):
        self.dir.cleanup()

    def test_rename_that_left_the_old_name_is_flagged(self):
        out, n = name_check.annotate(body(
            self.path, "def scale_points(points, k, origin=(0.0, 0.0)):",
            "def scale_points(points: list, factor: float, origin=(0.0, 0.0)):"))
        self.assertEqual(n, 1)
        note = out["messages"][-1]["content"][0]["content"][-1]["text"]
        self.assertIn("`k` in `scale_points`", note)

    def test_note_persists_and_is_repeated_on_the_newest_result(self):
        b = later(body(self.path, "def scale_points(points, k, origin=(0.0, 0.0)):",
                       "def scale_points(points: list, factor: float, origin=(0.0, 0.0)):"), self.path)
        out, n = name_check.annotate(b)
        self.assertEqual(n, 1)
        self.assertIn("NameError", out["messages"][2]["content"][0]["content"][-1]["text"])
        self.assertIn("Still undefined", out["messages"][-1]["content"][0]["content"][-1]["text"])

    def test_fixed_name_drops_out(self):
        with open(self.path, "w") as fh:
            fh.write(AFTER.replace("* k", "* factor"))
        b = later(body(self.path, "def scale_points(points, k, origin=(0.0, 0.0)):",
                       "def scale_points(points: list, factor: float, origin=(0.0, 0.0)):"), self.path)
        self.assertEqual(name_check.annotate(b)[1], 0)

    def test_name_undefined_before_the_edit_is_not_flagged(self):
        # The edit touched a different line of the function; `k` was already undefined.
        out, n = name_check.annotate(body(self.path, "    ox, oy = origin\n", "    ox, oy = origin\n"
                                          .replace("origin", "origin ")))
        self.assertEqual(n, 0)

    def test_edit_elsewhere_is_not_flagged(self):
        _, n = name_check.annotate(body(self.path, "factor=3.0", "factor=2.0"))
        self.assertEqual(n, 0)

    def test_failed_edit_is_not_checked(self):
        _, n = name_check.annotate(body(self.path, "def scale_points(points, k, origin=(0.0, 0.0)):",
                                        "def scale_points(points: list, factor: float, origin=(0.0, 0.0)):",
                                        is_error=True))
        self.assertEqual(n, 0)

    def test_clean_code_is_quiet(self):
        src = ("import os\nX = 1\nclass C:\n    def top(self, which):\n        return self.x(which)\n"
               "def f(a):\n    global Y\n    Y = [i * a for i in range(3)]\n    return os.sep, X, len(Y)\n"
               "def g():\n    return Y\n")
        self.assertEqual(name_check.undefined(src, "c.py"), [])

    def test_comprehension_name_is_attributed_to_its_function(self):
        self.assertEqual(name_check.undefined(AFTER, "s.py"), [("scale_points", 1, "k")])

    def test_lambda_and_comprehension_edits_are_seen_in_their_function(self):
        src = ("def f(xs):\n    g = lambda x: x * k\n    a = {x: m for x in xs}\n"
               "    b = {x * n for x in xs}\n    return g, a, b\n")
        found = name_check.undefined(src, "f.py", lines={2, 3, 4})
        self.assertEqual(sorted(n for _, _, n in found), ["k", "m", "n"])
        self.assertEqual({f for f, _, _ in found}, {"f"})

    def test_local_variable_annotation_is_not_evaluated(self):
        src = "def f():\n    x: Undef = 1\n    y: Other = 2\n    return x, y, Other\n"
        self.assertEqual(name_check.undefined(src, "f.py"), [("f", 1, "Other")])


if __name__ == "__main__":
    unittest.main()

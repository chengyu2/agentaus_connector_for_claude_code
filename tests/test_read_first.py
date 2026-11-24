"""A Read put ahead of an Edit/Write that Claude Code would refuse. See read_first.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import read_first  # noqa: E402
from agentaus_bridge.server import _reads_first, _with_reads_first  # noqa: E402

TOOLS = [{"name": n, "input_schema": {"type": "object"}} for n in ("Read", "Edit", "Write", "Bash")]


def call(name, **args):
    return {"id": f"c_{name}_{len(args)}_{time.monotonic_ns()}", "name": name,
            "arguments": json.dumps(args)}


def turn(name, path, ok=True, cid="t1"):
    return [
        {"role": "assistant", "content": [{"type": "tool_use", "id": cid, "name": name,
                                           "input": {"file_path": path}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid,
                                      "is_error": not ok, "content": "ok"}]},
    ]


class ReadFirst(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a.py")
        with open(self.path, "w") as fh:
            fh.write("x = 1\n")

    def tearDown(self):
        self.dir.cleanup()

    def messages(self, *turns):
        out = [{"role": "user", "content": "fix it"}]
        for t in turns:
            out += t
        return out

    def test_unread_edit_gets_a_read_first(self):
        edit = call("Edit", file_path=self.path, old_string="x = 1", new_string="x = 2")
        out, added = read_first.prepend_reads([edit], self.messages())
        self.assertEqual([c["name"] for c in out], ["Read", "Edit"])
        self.assertEqual(json.loads(out[0]["arguments"]), {"file_path": self.path})
        self.assertEqual(added, [(self.path, "not read yet")])
        self.assertIs(out[1], edit)

    def test_read_edit_is_left_alone(self):
        edit = call("Edit", file_path=self.path, old_string="x = 1", new_string="x = 2")
        msgs = self.messages(turn("Read", self.path))
        read_first.observe(msgs)
        out, added = read_first.prepend_reads([edit], msgs)
        self.assertEqual(out, [edit])
        self.assertEqual(added, [])

    def test_failed_read_does_not_count(self):
        edit = call("Edit", file_path=self.path, old_string="x = 1", new_string="x = 2")
        out, _ = read_first.prepend_reads([edit], self.messages(turn("Read", self.path, ok=False)))
        self.assertEqual([c["name"] for c in out], ["Read", "Edit"])

    def test_new_file_write_needs_no_read(self):
        write = call("Write", file_path=os.path.join(self.dir.name, "new.py"), content="y")
        out, _ = read_first.prepend_reads([write], self.messages())
        self.assertEqual(out, [write])

    def test_model_read_in_same_response_counts(self):
        read = call("Read", file_path=self.path)
        edit = call("Edit", file_path=self.path, old_string="x = 1", new_string="x = 2")
        out, _ = read_first.prepend_reads([read, edit], self.messages())
        self.assertEqual(out, [read, edit])

    def test_two_edits_get_one_read(self):
        e1 = call("Edit", file_path=self.path, old_string="x = 1", new_string="x = 2")
        e2 = call("Edit", file_path=self.path, old_string="x = 2", new_string="x = 3")
        out, _ = read_first.prepend_reads([e1, e2], self.messages())
        self.assertEqual([c["name"] for c in out], ["Read", "Edit", "Edit"])

    def test_changed_on_disk_since_read(self):
        msgs = self.messages(turn("Read", self.path))
        read_first.observe(msgs)
        later = time.time() + 5
        os.utime(self.path, (later, later))
        edit = call("Edit", file_path=self.path, old_string="x = 1", new_string="x = 2")
        out, added = read_first.prepend_reads([edit], msgs)
        self.assertEqual([c["name"] for c in out], ["Read", "Edit"])
        self.assertEqual(added[0][1], "changed on disk since read")

    def test_large_file_gets_a_one_line_read(self):
        with open(self.path, "w") as fh:
            fh.write("x = 1\n" * 20000)
        edit = call("Edit", file_path=self.path, old_string="x", new_string="y")
        out, _ = read_first.prepend_reads([edit], self.messages())
        self.assertEqual(json.loads(out[0]["arguments"]).get("limit"), 1)

    def test_server_needs_read_offered(self):
        edit = call("Edit", file_path=self.path, old_string="x = 1", new_string="x = 2")
        body = {"messages": self.messages(), "tools": TOOLS}
        self.assertEqual([c["name"] for c in _reads_first([edit], body)], ["Read", "Edit"])
        body = {"messages": self.messages(), "tools": TOOLS[1:]}
        self.assertEqual(_reads_first([edit], body), [edit])

    def test_non_streaming_response_gets_the_read(self):
        edit = call("Edit", file_path=self.path, old_string="x = 1", new_string="x = 2")
        data = {"choices": [{"message": {"tool_calls": [
            {"id": edit["id"], "type": "function",
             "function": {"name": "Edit", "arguments": edit["arguments"]}}]}}]}
        out = _with_reads_first(data, [edit], {"messages": self.messages(), "tools": TOOLS})
        names = [c["function"]["name"] for c in out["choices"][0]["message"]["tool_calls"]]
        self.assertEqual(names, ["Read", "Edit"])


class PythonIsolation(unittest.TestCase):
    SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}},
              "required": ["command"]}

    def run_repair(self, command, cwd):
        from agentaus_bridge import repair
        out = repair.repair({"name": "Bash", "arguments": json.dumps({"command": command})},
                            "Bash", self.SCHEMA, cwd)
        return json.loads(out["arguments"])["command"]

    def test_local_import_drops_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "solutions.py"), "w").close()
            cmd = "python3 -I - <<'EOF'\nfrom solutions import *\nprint(1)\nEOF"
            self.assertTrue(self.run_repair(cmd, d).startswith("python3 -E -s - <<'EOF'"))

    def test_script_with_local_import_drops_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "pkg"))
            open(os.path.join(d, "pkg", "__init__.py"), "w").close()
            with open(os.path.join(d, "run.py"), "w") as fh:
                fh.write("from pkg import x\n")
            self.assertEqual(self.run_repair("python -I run.py", d), "python -E -s run.py")

    def test_no_local_import_keeps_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = "python3 -I - <<'EOF'\nimport json\nEOF"
            self.assertEqual(self.run_repair(cmd, d), cmd)


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest

from agentaus_bridge import finish


def _use(i, name, **inp):
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}


def _res(i, text, err=False):
    return {"type": "tool_result", "tool_use_id": f"t{i}", "content": text, "is_error": err}


def convo(*steps, request="Fix the bug in `app.py`."):
    msgs = [{"role": "user", "content": request}]
    for i, (call, result) in enumerate(steps):
        msgs.append({"role": "assistant", "content": [_use(i, call[0], **call[1])]})
        msgs.append({"role": "user", "content": [_res(i, result)]})
    return msgs


class VerificationTests(unittest.TestCase):
    def test_edit_without_run_is_unverified(self):
        m = convo((("Read", {"file_path": "/w/app.py"}), "x"),
                  (("Edit", {"file_path": "/w/app.py"}), "ok"))
        self.assertEqual(finish.verification_state(m)["unrun"], ["/w/app.py"])

    def test_run_after_edit_clears(self):
        m = convo((("Edit", {"file_path": "/w/app.py"}), "ok"),
                  (("Bash", {"command": "python -m pytest -q"}), "3 passed"))
        st = finish.verification_state(m)
        self.assertEqual(st["unrun"], [])
        self.assertIsNone(st["failed"])

    def test_looking_is_not_running(self):
        m = convo((("Edit", {"file_path": "/w/app.py"}), "ok"),
                  (("Bash", {"command": "cat app.py | head -20 && ls"}), "..."))
        self.assertEqual(finish.verification_state(m)["unrun"], ["/w/app.py"])

    def test_quoted_pipe_is_not_a_pipeline(self):
        self.assertFalse(finish.runs_something(
            'grep -n "k=" /w/s.py; echo "---"; grep -n "scale_points\\|def scale" /w/s.py'))
        self.assertFalse(finish.runs_something("cat a.py | grep -c def > /tmp/n"))
        self.assertTrue(finish.runs_something("cd /w && python -m pytest -q 2>&1 | tail -5"))
        self.assertTrue(finish.runs_something("ls; python3 -c 'import shapes'"))

    def test_doc_edits_are_not_gated(self):
        m = convo((("Write", {"file_path": "/w/answers.md"}), "ok"))
        self.assertEqual(finish.verification_state(m)["unrun"], [])

    def test_failed_pytest_run(self):
        out = "....F\n=========== 1 failed, 4 passed in 0.12s ===========\n"
        m = convo((("Edit", {"file_path": "/w/app.py"}), "ok"),
                  (("Bash", {"command": "python -m pytest tests"}), out))
        self.assertIn("1 failed", finish.verification_state(m)["failed"])

    def test_program_error_output_is_not_a_failed_run(self):
        out = "Error on line 3: division by zero\n"
        m = convo((("Edit", {"file_path": "/w/app.py"}), "ok"),
                  (("Bash", {"command": "python3 tally.py t.tl"}), out))
        self.assertIsNone(finish.verification_state(m)["failed"])

    def test_turn_starts_at_last_user_text(self):
        m = convo((("Edit", {"file_path": "/w/app.py"}), "ok"))
        m.append({"role": "assistant", "content": "done"})
        m.append({"role": "user", "content": "thanks, now explain it"})
        self.assertEqual(finish.verification_state(m)["unrun"], [])


class RequestTests(unittest.TestCase):
    def test_deliverables(self):
        self.assertEqual(finish.deliverables("Write `answers.json` with exactly these keys"),
                         ["answers.json"])
        self.assertEqual(finish.deliverables("Implement the interpreter in `tally.py`"), [])

    def test_missing_deliverable_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            req = "Write `answers.json` with the figures."
            self.assertEqual(finish.missing_deliverables(req, d, []), ["answers.json"])
            open(os.path.join(d, "answers.json"), "w").close()
            self.assertEqual(finish.missing_deliverables(req, d, []), [])

    def test_checklist(self):
        req = ("Two things:\n\n1. Fix the failing test suite so it passes.\n"
               "2. Fix the four tickets below.\n\n"
               "   > **Ticket 1 - \"Invoices off by a cent.\"** blah\n"
               "- ok\n")
        items = finish.checklist(req)
        self.assertEqual(len(items), 3)
        self.assertTrue(items[2].startswith("Ticket 1"))

    def test_authority_docs_and_range(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "FEATURE_SPEC.md"), "w").close()
            req = "Implement `FEATURE_SPEC.md`. Every numbered requirement (R1–R11) matters."
            self.assertEqual(finish.authority_docs(req, d), ["FEATURE_SPEC.md"])
            self.assertEqual(finish.requirement_range(req), "R1-R11")
            self.assertIn("R1-R11", finish.checklist_text(req, d))


class GateTests(unittest.TestCase):
    def setUp(self):
        finish._USED.clear()

    def test_unverified_gate_fires_twice_then_stops(self):
        m = convo((("Edit", {"file_path": "/w/app.py"}), "ok"))
        for _ in range(2):
            kind, text, force = finish.finish_gate("k", "Fix it.", m, None, can_run=True)
            self.assertEqual(kind, "unverified")
            self.assertTrue(force)
        self.assertEqual(finish.finish_gate("k", "Fix it.", m, None, can_run=True)[0], "")

    def test_checklist_once_after_work(self):
        req = "Do:\n1. first requirement here\n2. second requirement here\n3. third one here ok\n"
        m = convo((("Edit", {"file_path": "/w/app.py"}), "ok"),
                  (("Bash", {"command": "python app.py"}), "fine"), request=req)
        self.assertEqual(finish.finish_gate("c", req, m, None, can_run=True)[0], "checklist")
        self.assertEqual(finish.finish_gate("c", req, m, None, can_run=True)[0], "")

    def test_question_without_work_passes(self):
        req = "Explain:\n1. first thing to explain\n2. second thing to explain\n3. third thing ok\n"
        m = convo((("Read", {"file_path": "/w/app.py"}), "x"), request=req)
        self.assertEqual(finish.finish_gate("q", req, m, None, can_run=True)[0], "")

    def test_nudge_after_many_calls(self):
        req = "Write `answers.md` answering the questions."
        steps = [(("Read", {"file_path": f"/w/f{i}.py"}), "x") for i in range(20)]
        m = convo(*steps, request=req)
        self.assertIn("answers.md", finish.deliverable_nudge(req, None, m, after_calls=18))
        self.assertEqual(finish.deliverable_nudge(req, None, m[:10], after_calls=18), "")

    def test_rewrite_nudge_after_large_write(self):
        small = convo((("Write", {"file_path": "/w/a.py", "content": "x = 1\n"}), "ok"))
        self.assertEqual(finish.rewrite_nudge(small, "/w"), "")
        big = convo((("Write", {"file_path": "/w/a.py", "content": "x = 1\n" * 1000}), "ok"))
        note = finish.rewrite_nudge(big, "/w")
        self.assertIn("`a.py`", note)
        self.assertIn("Edit", note)


if __name__ == "__main__":
    unittest.main()

"""The three compensations that are about context rather than capability.

The ledger stops the model re-running what it already ran. Distillation stops one large
tool result eating the window. Investigation trades latency for corroboration. Each has
one property that matters more than the rest, and each is tested for it first.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import ledger, tools  # noqa: E402
from agentaus_bridge.distill import ResultDistiller  # noqa: E402
from agentaus_bridge.translate import estimate_tokens  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def call(id_: str, name: str, **kwargs) -> dict:
    return {"role": "assistant",
            "content": [{"type": "tool_use", "id": id_, "name": name, "input": kwargs}]}


def result(id_: str, body, **kwargs) -> dict:
    return {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": id_, "content": body, **kwargs}]}


class TestTheLedger(unittest.TestCase):
    def test_it_names_the_call_and_how_it_turned_out(self):
        text = ledger.render([
            call("a", "Read", file_path="config.py"),
            result("a", "x = 1"),
            call("b", "Bash", command="pytest -q"),
            result("b", "boom", is_error=True),
        ])
        self.assertIn("Read(config.py) -> ok", text)
        self.assertIn("Bash(pytest -q) -> error", text)

    def test_an_unanswered_call_is_pending_not_successful(self):
        """A call still in flight has told the model nothing yet."""
        text = ledger.render([call("a", "Grep", pattern="semaphore")])
        self.assertIn("-> pending", text)

    def test_an_empty_result_is_distinguished_from_a_useful_one(self):
        """The failure this guards: a search that found nothing, read as an answer."""
        text = ledger.render([call("a", "Grep", pattern="nope"), result("a", "")])
        self.assertIn("-> empty", text)

    def test_it_costs_nothing_when_no_tools_have_run(self):
        self.assertEqual(ledger.render([{"role": "user", "content": "hi"}]), "")
        self.assertEqual(ledger.with_ledger("sys", []), "sys")

    def test_old_entries_are_counted_rather_than_listed(self):
        messages = []
        for i in range(60):
            messages += [call(f"c{i}", "Read", file_path=f"f{i}.py"), result(f"c{i}", "x")]
        text = ledger.render(messages, limit=10)
        self.assertIn("60 call(s)", text)
        self.assertIn("50 older not listed", text)
        self.assertIn("f59.py", text)
        self.assertNotIn("f0.py", text)

    def test_it_is_derived_not_stored(self):
        """Same input, same output, with no state carried between calls."""
        messages = [call("a", "Read", file_path="x.py"), result("a", "x")]
        self.assertEqual(ledger.render(messages), ledger.render(messages))

    def test_a_long_input_is_digested_rather_than_dumped(self):
        text = ledger.render([call("a", "Bash", command="echo " + "x" * 500)])
        self.assertLess(len(text.splitlines()[-1]), 140)


class TestDistillation(unittest.TestCase):
    BIG = "def handler(x):\n    return x.strip()\n" * 900

    def stub(self, answer="`handler` at line 1, repeated.\n[elided: 899 identical defs]"):
        seen = []

        async def summarise(prompt: str) -> str:
            seen.append(prompt)
            return answer

        summarise.seen = seen  # type: ignore[attr-defined]
        return summarise

    def test_a_large_result_is_condensed(self):
        stub = self.stub()
        d = ResultDistiller(stub, threshold_tokens=500, chunk_tokens=4000)
        body = {"messages": [call("a", "Read", file_path="big.py"), result("a", self.BIG)]}

        before = estimate_tokens(self.BIG)
        out = run(d.distill(body))
        after = estimate_tokens(out["messages"][1]["content"][0]["content"])

        self.assertLess(after, before)
        self.assertIn("Condensed by the bridge", out["messages"][1]["content"][0]["content"])

    def test_the_distiller_is_told_which_tool_produced_the_output(self):
        stub = self.stub()
        d = ResultDistiller(stub, threshold_tokens=500)
        run(d.distill({"messages": [call("a", "Read", file_path="big.py"),
                                    result("a", self.BIG)]}))
        self.assertIn("name: Read", stub.seen[0])
        self.assertIn("big.py", stub.seen[0])

    def test_it_is_cached_so_the_prefix_stays_stable(self):
        """The property the compaction cache depends on.

        Tool results are re-sent every turn. If distillation recomputed - or returned
        anything different - the conversation prefix would change on every request and
        the compactor would re-summarise the whole history each time.
        """
        stub = self.stub()
        d = ResultDistiller(stub, threshold_tokens=500)
        body = {"messages": [call("a", "Read", file_path="big.py"), result("a", self.BIG)]}

        first = run(d.distill(body))
        calls_after_first = len(stub.seen)
        second = run(d.distill(body))

        self.assertEqual(len(stub.seen), calls_after_first, "it recomputed on turn two")
        self.assertEqual(first["messages"][1]["content"][0]["content"],
                         second["messages"][1]["content"][0]["content"])
        self.assertEqual(d.hits, 1)

    def test_small_results_are_left_exactly_alone(self):
        stub = self.stub()
        d = ResultDistiller(stub, threshold_tokens=500)
        body = {"messages": [call("a", "Read", file_path="x.py"), result("a", "x = 1")]}
        self.assertIs(run(d.distill(body)), body)
        self.assertEqual(stub.seen, [])

    def test_errors_are_never_condensed(self):
        """The exact text of a failure is usually the thing being debugged."""
        stub = self.stub()
        d = ResultDistiller(stub, threshold_tokens=100)
        body = {"messages": [call("a", "Bash", command="pytest"),
                             result("a", "Traceback\n" * 400, is_error=True)]}
        out = run(d.distill(body))
        self.assertIs(out, body)
        self.assertEqual(stub.seen, [])

    def test_a_failing_distiller_keeps_the_raw_result(self):
        """Worse for the window, but correct. Failing the turn would be a bad trade."""
        async def broken(_: str) -> str:
            raise RuntimeError("upstream down")

        d = ResultDistiller(broken, threshold_tokens=500)
        body = {"messages": [call("a", "Read", file_path="big.py"), result("a", self.BIG)]}
        out = run(d.distill(body))
        self.assertEqual(out["messages"][1]["content"][0]["content"], self.BIG)

    def test_a_distillation_that_grew_is_discarded(self):
        d = ResultDistiller(self.stub(answer="x" * 200_000), threshold_tokens=500)
        body = {"messages": [call("a", "Read", file_path="big.py"), result("a", self.BIG)]}
        out = run(d.distill(body))
        self.assertEqual(out["messages"][1]["content"][0]["content"], self.BIG)

    def test_typography_is_normalised(self):
        d = ResultDistiller(self.stub(answer="see $$retry_budget_ms$$ in EU‑WEST‑2"),
                            threshold_tokens=500)
        body = {"messages": [call("a", "Read", file_path="big.py"), result("a", self.BIG)]}
        out = run(d.distill(body))
        text = out["messages"][1]["content"][0]["content"]
        self.assertIn("`retry_budget_ms`", text)
        self.assertIn("EU-WEST-2", text)


class TestInvestigate(unittest.TestCase):
    def test_it_runs_every_lens_and_corroborates(self):
        seen = []

        async def call_(prompt: str) -> str:
            seen.append(prompt)
            if "list the literal strings" in prompt.lower():
                return "Semaphore"
            if "<excerpt file=" in prompt:
                return "12: _gate = asyncio.Semaphore(6)"
            if "Three independent searches" in prompt:
                return "## Established\n- `gate.py:12`\n\n## Single-source\nnone"
            return "NONE"

        import tempfile
        with tempfile.TemporaryDirectory() as tree:
            with open(os.path.join(tree, "gate.py"), "w") as fh:
                fh.write("import asyncio\n_gate = asyncio.Semaphore(6)\n")
            out = run(tools.run_investigate("where is the cap", tree, call_))

        angles = [p for p in seen if p.startswith("A developer is searching")]
        self.assertEqual(len(angles), 3, "not every lens ran")
        self.assertIn("Established", out)

    def test_a_single_surviving_lens_is_not_presented_as_agreement(self):
        """One source is not corroboration, and must not be dressed up as it."""
        import tempfile

        async def call_(prompt: str) -> str:
            if "list the literal strings" in prompt.lower():
                return "alpha"
            if "<excerpt file=" in prompt:
                # Only the "definition" lens finds anything.
                return "1: alpha = 1" if "Specifically: Where is this implemented" in prompt else "NONE"
            return "merged"

        with tempfile.TemporaryDirectory() as tree:
            with open(os.path.join(tree, "a.py"), "w") as fh:
                fh.write("alpha = 1\n")
            out = run(tools.run_investigate("where is alpha", tree, call_))

        self.assertIn("nothing here is corroborated", out)
        self.assertNotIn("## Established", out)

    def test_nothing_found_says_so(self):
        import tempfile

        async def call_(prompt: str) -> str:
            return "alpha" if "list the literal strings" in prompt.lower() else "NONE"

        with tempfile.TemporaryDirectory() as tree:
            with open(os.path.join(tree, "a.py"), "w") as fh:
                fh.write("alpha = 1\n")
            out = run(tools.run_investigate("where is the parser", tree, call_))
        self.assertIn("None of the", out)

    def test_a_relative_path_is_refused(self):
        async def call_(_: str) -> str:
            raise AssertionError("should not be called")
        self.assertIn("absolute path", run(tools.run_investigate("q", "rel/path", call_)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

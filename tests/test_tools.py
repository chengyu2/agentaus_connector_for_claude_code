"""Bridge-executed search.

The point of this tool is that it finds things a regex would not, so the test that
matters most is the one where the query and the answer share no words at all. The rest
guard the ways a search can fail quietly: dropping hits it did not report, corrupting
the paths it returns, or reading files it should never open.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import gate, tools  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Tree:
    """A throwaway source tree."""

    def __init__(self, files: dict):
        self.dir = tempfile.TemporaryDirectory()
        for name, body in files.items():
            path = os.path.join(self.dir.name, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)

    @property
    def path(self) -> str:
        return self.dir.name

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.dir.cleanup()


def responder(*, terms: str = "semaphore\nconcurrency", hit_on: str = "", answer: str = "found it"):
    """A stub Agentaus. Records prompts and answers expansion vs chunk calls apart."""
    seen: list[str] = []

    async def call(prompt: str) -> str:
        seen.append(prompt)
        if prompt.startswith("A developer is searching"):
            return terms
        if hit_on and hit_on in prompt:
            return answer
        return "NONE"

    call.seen = seen  # type: ignore[attr-defined]
    return call


class TestSearchFindsWhatRegexCannot(unittest.TestCase):
    def test_finds_a_file_the_query_words_never_appear_in(self):
        """The reason this tool exists.

        "where do we cap concurrent calls" shares no word with `asyncio.Semaphore(6)`.
        A regex over the question finds nothing; expansion supplies the term that does.
        """
        with _Tree({
            "gate.py": "import asyncio\n_limit = asyncio.Semaphore(6)\n",
            "readme.md": "# project\nnothing relevant here\n",
        }) as tree:
            call = responder(terms="Semaphore\nasyncio", hit_on="gate.py",
                             answer="2: _limit = asyncio.Semaphore(6)")
            out = run(tools.run_search(
                "where do we cap concurrent calls", tree.path, None, call))

        self.assertIn("Semaphore(6)", out)
        self.assertIn("gate.py", out)

    def test_a_thin_shortlist_falls_back_to_reading_everything(self):
        """A prefilter that matches nothing means the words are absent, not the answer."""
        with _Tree({
            "a.py": "def alpha():\n    return 1\n",
            "b.py": "def beta():\n    return 2\n",
        }) as tree:
            # Expansion returns a term present in no file, so nothing is shortlisted.
            call = responder(terms="zzzznotpresent", hit_on="b.py", answer="2: return 2")
            out = run(tools.run_search("what does it return", tree.path, None, call))

        self.assertIn("b.py", out)
        chunk_prompts = [p for p in call.seen if p.startswith("Below is an excerpt")]
        self.assertEqual(len(chunk_prompts), 2, "brute force did not read every file")


class TestResultsAreReportedHonestly(unittest.TestCase):
    def test_none_answers_are_dropped(self):
        with _Tree({"a.py": "x = 1\n", "b.py": "y = 2\n"}) as tree:
            call = responder(terms="x\ny", hit_on="a.py", answer="1: x = 1")
            out = run(tools.run_search("where is x", tree.path, None, call))
        self.assertIn("a.py", out)
        self.assertNotIn("b.py", out)

    def test_no_hits_says_so_rather_than_returning_nothing(self):
        with _Tree({"a.py": "x = 1\n"}) as tree:
            call = responder(terms="x", hit_on="nothing-matches-this")
            out = run(tools.run_search("where is the parser", tree.path, None, call))
        self.assertIn("No excerpt", out)
        self.assertIn("Searched", out)

    def test_the_chunk_cap_truncates_and_says_so(self):
        """A silent cap reads as full coverage, which is worse than a stated partial."""
        files = {f"f{i}.py": f"value = {i}\n" for i in range(12)}
        previous = settings.agentaus_search_max_chunks
        settings.agentaus_search_max_chunks = 4
        try:
            with _Tree(files) as tree:
                call = responder(terms="value", hit_on="f0.py", answer="1: value = 0")
                out = run(tools.run_search("where is value", tree.path, None, call))
        finally:
            settings.agentaus_search_max_chunks = previous

        self.assertIn("4 of 12", out)
        self.assertIn("limit", out)

    def test_identifiers_survive_the_models_typography(self):
        """Agentaus rewrites ASCII hyphens and wraps identifiers in $$.

        In prose that is cosmetic. In a search result it corrupts every path returned,
        and a path that looks right and is not is worse than no answer.
        """
        with _Tree({"a.py": "x = 1\n"}) as tree:
            async def call(prompt: str) -> str:
                if prompt.startswith("A developer is searching"):
                    return "x"
                return "1: see $$retry_budget_ms$$ in EU‑WEST‑2"
            out = run(tools.run_search("where is x", tree.path, None, call))

        self.assertIn("`retry_budget_ms`", out)
        self.assertIn("EU-WEST-2", out)
        self.assertNotIn("‑", out)


class TestWhatItRefusesToRead(unittest.TestCase):
    def test_secrets_are_never_read_even_when_they_match(self):
        with _Tree({
            ".env": "AGENTAUS_API_KEY=secret-value\n",
            "server.pem": "-----BEGIN PRIVATE KEY-----\nsecret-value\n",
            "app.py": "key = load()  # secret-value\n",
        }) as tree:
            files = tools.enumerate_files(tree.path)
        names = {os.path.basename(f) for f in files}
        self.assertEqual(names, {"app.py"})

    def test_noise_directories_are_pruned(self):
        with _Tree({
            "src/app.py": "x = 1\n",
            "node_modules/pkg/index.js": "module.exports = 1\n",
            ".venv/lib/thing.py": "y = 2\n",
        }) as tree:
            files = tools.enumerate_files(tree.path)
        self.assertEqual([os.path.basename(f) for f in files], ["app.py"])

    def test_a_relative_path_is_refused_rather_than_guessed_at(self):
        call = responder()
        out = run(tools.run_search("anything", "relative/path", None, call))
        self.assertIn("absolute path", out)

    def test_search_roots_confine_the_bridge(self):
        with _Tree({"a.py": "x = 1\n"}) as tree:
            previous = settings.agentaus_search_roots
            settings.agentaus_search_roots = "/nonexistent-root"
            try:
                out = run(tools.run_search("where is x", tree.path, None, responder()))
            finally:
                settings.agentaus_search_roots = previous
        self.assertIn("AGENTAUS_SEARCH_ROOTS", out)

    def test_a_failing_tool_becomes_output_rather_than_an_exception(self):
        async def broken(_: str) -> str:
            raise RuntimeError("upstream is down")

        with _Tree({"a.py": "x = 1\n"}) as tree:
            # Expansion failing must not end the turn; it falls back to the query's
            # own words and carries on.
            out = run(tools.execute(
                tools.SEARCH_TOOL, {"query": "where is x", "path": tree.path}, broken))
        self.assertIsInstance(out, str)
        self.assertTrue(out)


class TestConcurrency(unittest.TestCase):
    def test_search_respects_the_shared_cap(self):
        in_flight = {"now": 0, "peak": 0}

        async def slow(prompt: str) -> str:
            if prompt.startswith("A developer is searching"):
                return "value"
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            await asyncio.sleep(0.01)
            in_flight["now"] -= 1
            return "NONE"

        files = {f"f{i}.py": f"value = {i}\n" for i in range(20)}
        previous = (settings.agentaus_max_concurrency, settings.max_concurrency_is_explicit)
        settings.agentaus_max_concurrency = 3
        settings.max_concurrency_is_explicit = True
        gate.reset()
        try:
            with _Tree(files) as tree:
                run(tools.run_search("where is value", tree.path, None, slow))
        finally:
            settings.agentaus_max_concurrency, settings.max_concurrency_is_explicit = previous
            gate.reset()

        self.assertGreater(in_flight["peak"], 1, "search ran one chunk at a time")
        self.assertLessEqual(in_flight["peak"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWebSearch(unittest.TestCase):
    """Agentaus' own web search, exposed as a callable tool.

    It is triggered by the phrase "web search this" in the prompt rather than by a
    parameter, so the thing worth asserting is that the phrase is actually sent - the
    tool is otherwise indistinguishable from an ordinary completion.
    """

    def test_the_trigger_phrase_is_sent(self):
        seen = []

        async def call(prompt: str) -> str:
            seen.append(prompt)
            return "httpx 0.28 was released in 2024. Source: https://example.invalid"

        out = run(tools.run_web_search("latest httpx release", call))

        self.assertTrue(seen[0].startswith("web search this:"),
                        "Agentaus' web search is prompt-triggered; without the phrase "
                        "it answers from memory instead of searching")
        self.assertIn("latest httpx release", seen[0])
        self.assertIn("httpx 0.28", out)

    def test_it_is_asked_to_cite_and_not_to_answer_from_memory(self):
        seen = []

        async def call(prompt: str) -> str:
            seen.append(prompt)
            return "ok"

        run(tools.run_web_search("anything", call))
        self.assertIn("source URL", seen[0])
        self.assertIn("not from memory", seen[0])

    def test_typography_is_normalised_like_every_other_answer(self):
        async def call(_: str) -> str:
            return "see $$retry_budget_ms$$ in EU‑WEST‑2"

        out = run(tools.run_web_search("q", call))
        self.assertIn("`retry_budget_ms`", out)
        self.assertIn("EU-WEST-2", out)

    def test_an_empty_query_is_refused(self):
        async def call(_: str) -> str:
            raise AssertionError("should not have been called")

        self.assertIn("needs a query", run(tools.run_web_search("   ", call)))

    def test_a_failure_becomes_output_rather_than_an_exception(self):
        async def broken(_: str) -> str:
            raise RuntimeError("upstream is down")

        out = run(tools.execute(tools.WEB_SEARCH_TOOL, {"query": "x"}, broken))
        self.assertIn("web search failed", out)

    def test_an_empty_result_says_so_rather_than_returning_nothing(self):
        async def empty(_: str) -> str:
            return "   "

        out = run(tools.run_web_search("obscure thing", empty))
        self.assertIn("nothing usable", out)

    def test_it_is_dispatched_by_name(self):
        async def call(prompt: str) -> str:
            return "answered"

        out = run(tools.execute(tools.WEB_SEARCH_TOOL, {"query": "x"}, call))
        self.assertEqual(out, "answered")

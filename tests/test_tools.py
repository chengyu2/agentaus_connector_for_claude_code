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
        if "<question>" in prompt and "<excerpt" not in prompt and "list the literal strings" in prompt.lower():
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
        chunk_prompts = [p for p in call.seen if "<excerpt file=" in p]
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
                if "<question>" in prompt and "<excerpt" not in prompt and "list the literal strings" in prompt.lower():
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
            if "<question>" in prompt and "<excerpt" not in prompt and "list the literal strings" in prompt.lower():
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


class TestBinaryFormatsAreSkipped(unittest.TestCase):
    """Genuinely unreadable formats are still walked past.

    Office documents used to be in this list. They are not any more - LibreOffice reads
    them (see test_documents.py) - because a tender response or requirements matrix is
    exactly the material worth searching, and skipping it was never what anyone wanted.
    """

    def test_binary_data_files_are_not_enumerated(self):
        with _Tree({
            "notes.md": "the real content\n",
            "cache.sqlite": "SQLite format 3",
            "weights.npy": "\x93NUMPY",
            "data.parquet": "PAR1",
        }) as tree:
            names = {os.path.basename(f) for f in tools.enumerate_files(tree.path)}
        self.assertEqual(names, {"notes.md"})

    def test_office_documents_are_enumerated_when_they_can_be_read(self):
        from agentaus_bridge import documents
        if not documents.available():
            self.skipTest("LibreOffice is not installed here")
        with _Tree({"notes.md": "x\n", "response.docx": "PK\x03\x04"}) as tree:
            names = {os.path.basename(f) for f in tools.enumerate_files(tree.path)}
        self.assertIn("response.docx", names,
                      "an office document was skipped despite being readable")

    def test_office_documents_are_skipped_when_they_cannot(self):
        from agentaus_bridge import documents
        from agentaus_bridge.config import settings
        previous = settings.agentaus_office_extract
        settings.agentaus_office_extract = False
        try:
            with _Tree({"notes.md": "x\n", "response.docx": "PK\x03\x04"}) as tree:
                names = {os.path.basename(f) for f in tools.enumerate_files(tree.path)}
        finally:
            settings.agentaus_office_extract = previous
        self.assertEqual(names, {"notes.md"})


class TestSelectivity(unittest.TestCase):
    def test_a_term_in_everything_is_dropped(self):
        texts = ["alpha document", "beta document", "gamma document", "delta document"]
        self.assertEqual(tools.selective_terms(texts, ["document", "alpha"]), ["alpha"])

    def test_all_ubiquitous_terms_are_kept_rather_than_ranking_on_nothing(self):
        texts = ["document", "document", "document"]
        self.assertEqual(tools.selective_terms(texts, ["document"]), ["document"])

    def test_measured_over_chunks_not_files(self):
        """The single-file case. Selectivity across one file filters nothing, which is
        why a 434 KB document was read 29 chunks out of 29."""
        chunks = ["intro document blah", "document about ingestion",
                  "document about OCR", "SEMAPHORE lives here document"]
        self.assertEqual(
            tools.selective_terms(chunks, ["document", "semaphore".upper()]),
            ["SEMAPHORE"],
        )


class TestZoom(unittest.TestCase):
    """A search hit proves a fact exists; it does not give enough to write from.

    Observed on a real tender: asked to append substantive clauses backed by evidence,
    the model produced "(evidence: lines 3585-3586)" - a citation where a sentence was
    wanted, because a citation was all it had.
    """

    DOC = "\n".join(
        ["# Introduction", "intro prose", ""]
        + [f"filler line {i}" for i in range(40)]
        + ["", "## Security", "The core service holds ISO/IEC 27001 certification.",
           "It also meets Essential 8 Maturity Level 2.", "Hosted in Vault Cloud.", ""]
        + [f"tail line {i}" for i in range(40)]
        + ["", "## Unrelated", "nothing to see"]
    )

    def stub(self, answer="kept lines"):
        seen = []
        async def call(prompt: str) -> str:
            seen.append(prompt)
            return answer
        call.seen = seen  # type: ignore[attr-defined]
        return call

    def test_a_citation_is_widened_to_its_section(self):
        with _Tree({"doc.md": self.DOC}) as tree:
            path = os.path.join(tree.path, "doc.md")
            cited = self.DOC.splitlines().index(
                "The core service holds ISO/IEC 27001 certification.") + 1
            out = run(tools.run_zoom(path, cited, cited, "", self.stub()))

        self.assertIn("## Security", out, "the heading that names the passage was lost")
        self.assertIn("Essential 8 Maturity Level 2", out, "neighbouring evidence was lost")
        self.assertNotIn("## Unrelated", out, "it ran past the end of the section")

    def test_line_numbers_are_preserved_so_citations_stay_valid(self):
        with _Tree({"doc.md": self.DOC}) as tree:
            path = os.path.join(tree.path, "doc.md")
            cited = self.DOC.splitlines().index("Hosted in Vault Cloud.") + 1
            out = run(tools.run_zoom(path, cited, cited, "", self.stub()))
        line = next(l for l in out.splitlines() if "Vault Cloud" in l)
        self.assertTrue(line.strip().startswith(str(cited)),
                        f"line number missing or wrong: {line!r}")

    def test_it_returns_verbatim_when_it_fits(self):
        """Condensing a passage the caller is about to quote defeats the point."""
        call = self.stub()
        with _Tree({"doc.md": self.DOC}) as tree:
            path = os.path.join(tree.path, "doc.md")
            run(tools.run_zoom(path, 5, 5, "", call))
        self.assertEqual(call.seen, [], "it called the model on a passage that already fit")

    # The window is trimmed to fit the budget now, so condensation only fires when the
    # CITED LINES THEMSELVES exceed it. One enormous line does that; a long section of
    # ordinary lines does not, because trimming handles it (see TestZoomIsNormallyFree).
    PROSE = "\n".join(["## Huge section", "y" * 200_000, "tail"])

    def test_an_oversized_section_is_condensed_against_the_purpose(self):
        call = self.stub(answer="     2  kept line\n[dropped: the rest of a huge line]")
        with _Tree({"doc.md": self.PROSE}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 2, 2,
                                     "find the certifications", call))
        self.assertEqual(len(call.seen), 1, "a passage over the budget was not condensed")
        self.assertIn("find the certifications", call.seen[0])
        self.assertIn("dropped", out)

    def test_a_failed_condensation_truncates_rather_than_losing_the_passage(self):
        async def broken(_: str) -> str:
            raise RuntimeError("upstream down")
        with _Tree({"doc.md": self.PROSE}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 2, 2, "x", broken))
        self.assertIn("truncated", out)
        self.assertIn("yyy", out, "the passage was lost rather than truncated")

    def test_a_line_past_the_end_says_so(self):
        with _Tree({"doc.md": "one\ntwo\n"}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 999, None, "", self.stub()))
        self.assertIn("past the end", out)

    def test_a_docx_is_refused_only_when_it_cannot_be_read(self):
        from agentaus_bridge.config import settings
        previous = settings.agentaus_office_extract
        settings.agentaus_office_extract = False
        try:
            with _Tree({"r.docx": "PK\x03\x04binary"}) as tree:
                out = run(tools.run_zoom(os.path.join(tree.path, "r.docx"), 1, 1, "",
                                         self.stub()))
        finally:
            settings.agentaus_office_extract = previous
        self.assertIn("no reader for it is", out)
        self.assertIn("brew install --cask libreoffice", out,
                      "it should say exactly how to fix it")
        self.assertIn("AGENTAUS_SOFFICE_PATH", out)

    def test_a_relative_path_falls_back_to_the_working_directory(self):
        with _Tree({"doc.md": self.DOC}) as tree:
            out = run(tools.run_zoom("doc.md", 2, 2, "", self.stub(), default_path=tree.path))
        self.assertIn("Introduction", out)

    def test_search_output_points_at_zoom(self):
        with _Tree({"a.py": "x = 1\n"}) as tree:
            call = responder(terms="x", hit_on="a.py", answer="1: x = 1")
            out = run(tools.run_search("where is x", tree.path, None, call))
        self.assertIn(tools.ZOOM_TOOL, out,
                      "a citation is only useful if the model knows it can open it")


class TestZoomWindowFloor(unittest.TestCase):
    """A boundary can sit two lines away.

    Tender documents use a bold single line as a sub-heading constantly, so honouring
    the nearest boundary alone returned three-line "sections" - technically correct and
    useless to write from. Observed live on the FIN response: "3 line(s) verbatim".
    """

    BOLD_HEAVY = "\n".join(
        [f"**Heading {i}**\nContent line for section {i}\nAnother line for {i}"
         for i in range(60)]
    )

    def stub(self):
        async def call(_: str) -> str:
            raise AssertionError("should not need the model for a small passage")
        return call

    def test_a_tiny_section_is_widened_to_something_readable(self):
        with _Tree({"doc.md": self.BOLD_HEAVY}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 90, 90, "", self.stub()))
        body = [l for l in out.splitlines() if l.strip() and not l.startswith("/")]
        self.assertGreaterEqual(len(body), 20,
                                f"still returned a {len(body)}-line sliver")

    def test_the_cited_line_stays_inside_the_window(self):
        with _Tree({"doc.md": self.BOLD_HEAVY}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 90, 90, "", self.stub()))
        nums = [int(l.split()[0]) for l in out.splitlines() if l.strip() and l.split()[0].isdigit()]
        self.assertLessEqual(min(nums), 90)
        self.assertGreaterEqual(max(nums), 90)

    def test_a_short_file_is_not_padded_past_its_end(self):
        with _Tree({"doc.md": "one\ntwo\nthree\n"}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 2, 2, "", self.stub()))
        nums = [int(l.split()[0]) for l in out.splitlines() if l.strip() and l.split()[0].isdigit()]
        self.assertLessEqual(max(nums), 3)


class TestLearnedCapacity(unittest.TestCase):
    """The safe prompt size is not a constant, so it is learned rather than assumed.

    48k tokens answered in 23 seconds when measured alone, then produced Cloudflare 524s
    under two concurrent searches. Retrying the same oversized prompt fails the same way -
    which is how a batch job burned four retries and reported failure with evidence
    sitting in the file all along.
    """

    def setUp(self):
        tools.reset_learned_capacity()

    def tearDown(self):
        tools.reset_learned_capacity()

    def test_a_timeout_is_recognised_as_a_capacity_problem(self):
        for message in ("Agentaus returned HTTP 524: <html>", "helper call timed out after 240s",
                        "HTTP 504 gateway timeout", "read operation timed out"):
            self.assertTrue(tools.is_capacity_failure(RuntimeError(message)), message)

    def test_a_real_error_is_not_mistaken_for_capacity(self):
        for message in ("HTTP 401 unauthorized", "summariser produced nothing",
                        "HTTP 400 invalid request"):
            self.assertFalse(tools.is_capacity_failure(RuntimeError(message)), message)

    def test_the_ceiling_halves_on_failure_and_is_remembered(self):
        self.assertEqual(tools.effective_chunk_tokens(),
                         settings.agentaus_search_chunk_tokens)
        tools.note_capacity_failure(40_000)
        self.assertEqual(tools.effective_chunk_tokens(), 20_000)
        tools.note_capacity_failure(20_000)
        self.assertEqual(tools.effective_chunk_tokens(), 10_000)

    def test_it_never_drops_below_something_answerable(self):
        for _ in range(20):
            tools.note_capacity_failure(tools.effective_chunk_tokens())
        self.assertGreaterEqual(tools.effective_chunk_tokens(), 3000)

    def test_a_later_failure_that_is_larger_does_not_raise_the_cap(self):
        tools.note_capacity_failure(8_000)
        self.assertEqual(tools.effective_chunk_tokens(), 4_000)
        tools.note_capacity_failure(60_000)
        self.assertEqual(tools.effective_chunk_tokens(), 4_000)

    def test_success_recovers_the_ceiling_gradually_then_lifts_it(self):
        tools.note_capacity_failure(16_000)
        capped = tools.effective_chunk_tokens()
        self.assertEqual(capped, 8_000)
        # Only a prompt near the current cap is evidence the upstream has recovered.
        tools.note_capacity_success(1_000)
        self.assertEqual(tools.effective_chunk_tokens(), capped)
        for _ in range(20):
            tools.note_capacity_success(tools.effective_chunk_tokens())
        self.assertEqual(tools.effective_chunk_tokens(),
                         settings.agentaus_search_chunk_tokens,
                         "the cap never lifted once large prompts were answered again")

    def test_an_oversized_chunk_is_split_rather_than_dropped(self):
        """The behaviour that matters: evidence in a timed-out chunk is still found."""
        calls = {"n": 0}

        async def flaky(prompt: str) -> str:
            if prompt.startswith("A developer is searching") or "list the literal" in prompt.lower():
                return "SEMAPHORE"
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Agentaus returned HTTP 524: origin timed out")
            return "12: found the SEMAPHORE here" if "SEMAPHORE" in prompt else "NONE"

        body = "\n".join(f"line {i}" for i in range(60) ) + "\nthe SEMAPHORE lives here\n"
        previous = settings.agentaus_search_chunk_tokens
        settings.agentaus_search_chunk_tokens = 40_000   # one chunk, so it must split
        try:
            with _Tree({"big.md": body}) as tree:
                out = run(tools.run_search("where is the cap", tree.path, None, flaky))
        finally:
            settings.agentaus_search_chunk_tokens = previous

        self.assertIn("SEMAPHORE", out, "the timed-out chunk was dropped instead of split")
        self.assertGreater(calls["n"], 1, "it never retried the halves")


class TestZoomIsNormallyFree(unittest.TestCase):
    """Zoom reads a file. It should almost never spend a model call.

    The threshold was 6000 tokens while the window it feeds is 131k, and a 400-line
    section of tender prose is ~24,000 characters - so every zoom exceeded it and
    condensed a passage that would have fitted untouched. Under load those calls took
    over 240s each and a repair run made no progress for an hour.
    """

    def test_a_full_size_section_of_real_prose_is_returned_verbatim(self):
        # 400 lines - the AGENTAUS_ZOOM_MAX_LINES ceiling - of tender-length sentences.
        prose = "\n".join(
            f"Paragraph {i}. " + "This sentence carries the sort of detail a tender "
            "response actually contains, at the length such documents run to. " * 3
            for i in range(400))

        async def must_not_be_called(_: str) -> str:
            raise AssertionError("zoom spent a model call on a passage that fits")

        with _Tree({"doc.md": prose}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 200, 200,
                                     "", must_not_be_called))
        self.assertIn("Paragraph 200", out)

    def test_the_ceiling_still_bites_when_the_cited_line_itself_is_huge(self):
        """Trimming cannot save a citation that is on its own bigger than the budget."""
        huge = "\n".join(["intro", "x" * 200_000, "tail"])
        seen = []

        async def condense(prompt: str) -> str:
            seen.append(prompt)
            return "     2  kept\n[dropped: the rest]"

        with _Tree({"doc.md": huge}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 2, 2, "why", condense))
        self.assertEqual(len(seen), 1, "an oversized citation was not condensed at all")
        self.assertIn("dropped", out)


class TestZoomWindowFitsTheConversation(unittest.TestCase):
    """A zoom result is read alongside everything else in the turn.

    At a 400-line ceiling, tender prose produced ~25,000-character passages - and two of
    those in one turn made the next upstream call large enough to draw a Cloudflare 524.
    Zoom has to be small enough to carry, not just cheap to produce.
    """

    def test_a_passage_stays_small_enough_to_send(self):
        prose = "\n".join(
            f"Paragraph {i}. " + "This sentence runs to the length tender prose runs to, "
            "carrying detail an evaluator expects to see. " * 3 for i in range(600))

        async def must_not_be_called(_: str) -> str:
            raise AssertionError("condensed a passage that should be returned verbatim")

        with _Tree({"doc.md": prose}) as tree:
            out = run(tools.run_zoom(os.path.join(tree.path, "doc.md"), 300, 300,
                                     "", must_not_be_called))

        self.assertIn("Paragraph 300", out, "the cited line fell outside the window")
        self.assertLess(len(out), 40_000,
                        f"zoom returned {len(out)} chars - too large to sit in a turn")
        # Still enough context to quote from, with neighbours either side.
        self.assertGreater(len(out), 3_000, "the window is too small to be useful")

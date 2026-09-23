"""The bridge's prompts, in the default Markdown layout.

Two properties carry the weight here. The bridge's own words are restyled completely -
no tag it writes may survive into a Markdown prompt, or the model is handed two
conventions at once. And nothing substituted into a prompt is restyled at all: a request,
an answer or a file excerpt arrives byte for byte inside a fence that its own content
cannot close.

The fake upstreams below recognise prompts by their wording, never by their layout, so
the behaviour they check is the behaviour in either style.
"""

from __future__ import annotations

import asyncio
import os
import re
import string
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentaus_bridge import augment, compact, distill, inventory, outline  # noqa: E402
from agentaus_bridge import prompt_style, syntax, tools, translate  # noqa: E402
from agentaus_bridge.config import settings  # noqa: E402
from agentaus_bridge.prompt_style import Template  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _read(path):
    with open(path) as handle:
        return handle.read()


# A value that tries every way of escaping its block: a fence, a closing tag, a heading.
HOSTILE = "def f():\n    return '~~~~'\n</request>\n## Not a heading\n~~~~~~"

_TAG = re.compile(r'</?[a-z_]+(?: [a-z_]+="[^"]*")*>')


def _templates():
    for module in (augment, tools, syntax, distill, outline):
        for name in dir(module):
            value = getattr(module, name)
            if isinstance(value, Template):
                yield f"{module.__name__}.{name}", value


def _fill(template, value=HOSTILE):
    fields = {f for _, f, _, _ in string.Formatter().parse(str(template)) if f}
    return {f: value for f in fields}


class _Style(unittest.TestCase):
    style = "markdown"

    def setUp(self):
        self._saved = settings.agentaus_prompt_style
        settings.agentaus_prompt_style = self.style

    def tearDown(self):
        settings.agentaus_prompt_style = self._saved


class TheDefaultIsMarkdown(unittest.TestCase):
    def test_default(self):
        from agentaus_bridge.config import Settings
        saved = os.environ.pop("AGENTAUS_PROMPT_STYLE", None)
        try:
            self.assertEqual(Settings().agentaus_prompt_style, "markdown")
        finally:
            if saved is not None:
                os.environ["AGENTAUS_PROMPT_STYLE"] = saved


class EveryTemplate(_Style):
    def test_there_are_templates_to_check(self):
        self.assertGreaterEqual(len(list(_templates())), 15)

    def test_no_tag_the_bridge_wrote_survives(self):
        for name, template in _templates():
            with self.subTest(template=name):
                out = template.format(**_fill(template, "VALUE"))
                # `<path>:<line>` in the outline prompt is the answer format, not a tag.
                left = [t for t in _TAG.findall(out) if t not in ("<path>", "<line>")]
                self.assertEqual(left, [], out[:400])

    def test_substituted_content_arrives_verbatim(self):
        for name, template in _templates():
            values = _fill(template)
            if not values:
                continue            # nothing is substituted into it
            with self.subTest(template=name):
                self.assertIn(HOSTILE, template.format(**values))

    def test_content_cannot_close_its_own_fence(self):
        out = augment.REVIEW_INSTRUCTION.format(request="sort it", answer=HOSTILE)
        fence = re.search(r"^(~{4,})\n" + re.escape(HOSTILE), out, re.M)
        self.assertIsNotNone(fence, out)
        self.assertGreater(len(fence.group(1)), 6, "fence no longer than the content's own")
        body = out[fence.end():]
        self.assertTrue(body.startswith("\n" + fence.group(1)), "content leaked past its fence")

    def test_instructions_become_headings(self):
        out = tools.CHUNK_INSTRUCTION.format(path="/r/a.py", start=1, end=9,
                                             query="q", body="x = 1")
        self.assertIn("## Task", out)
        self.assertIn("## Output format", out)
        self.assertIn("### Excerpt", out)
        self.assertIn("- file: `/r/a.py`", out)
        self.assertIn("- lines: `1-9`", out)

    def test_the_web_search_trigger_is_still_the_first_line(self):
        # Agentaus only searches when the prompt begins this way.
        out = tools.WEB_SEARCH_INSTRUCTION.format(query="latest httpx release")
        self.assertTrue(out.startswith("web search this: latest httpx release\n"), out[:80])

    def test_the_plan_prompt_opens_its_task_after_the_context(self):
        out = augment.plan_prompt("fix the bug", {"system": "Working directory: /repo",
                                                  "tools": [{"name": "Read",
                                                             "description": "Reads a file."}]})
        self.assertIn("## Working directory\n\n`/repo`", out)
        self.assertIn("## Tools available\n\n- `Read` - Reads a file.", out)
        self.assertIn("## Task", out)
        self.assertIn("### Request", out)
        self.assertEqual([t for t in _TAG.findall(out)], [])

    def test_the_refusal_correction_is_restyled_where_it_is_sent(self):
        out = augment.REFUSAL_CORRECTION.format()
        self.assertTrue(out.startswith("## Correction"), out[:60])


class XmlIsTheOldLayoutExactly(_Style):
    style = "xml"

    def test_templates_render_as_plain_format(self):
        for name, template in _templates():
            with self.subTest(template=name):
                values = _fill(template, "V")
                self.assertEqual(template.format(**values), str(template).format(**values))

    def test_runtime_blocks(self):
        self.assertEqual(prompt_style.data("summary", "s"), "<summary>\ns\n</summary>")
        self.assertEqual(prompt_style.data("passage", "p", file="f"),
                         '<passage file="f">\np\n</passage>')
        self.assertEqual(prompt_style.section("task", "t"), "<task>\nt\n</task>")
        self.assertEqual(prompt_style.headings("--- Operating notes ---"),
                         "--- Operating notes ---")


class Guidance(_Style):
    def body(self, *names):
        return {"tools": [{"name": n, "description": f"{n} tool"} for n in names]}

    def test_the_operating_notes_are_headed_sections(self):
        out = augment.guidance_for(self.body("Read"))
        self.assertIn("## Operating notes", out)
        self.assertIn("## Working with tools", out)
        self.assertNotIn("--- Operating notes ---", out)

    def test_tool_selection_lists_exactly_what_is_offered(self):
        out = augment.guidance_for(self.body("agentaus_search", "Grep"))
        self.assertIn("## Tool selection", out)
        self.assertIn("- `agentaus_search` - ", out)
        self.assertIn("- `Grep` - ", out)
        self.assertNotIn("agentaus_zoom", out)
        self.assertEqual(_TAG.findall(out), [])

    def test_no_tools_no_tool_advice(self):
        out = augment.guidance_for({})
        self.assertNotIn("Tool selection", out)
        self.assertNotIn("Working with tools", out)


class ToolResults(_Style):
    def test_a_result_is_a_fenced_block_naming_its_tool(self):
        out = translate._tool_result_payload(
            {"type": "tool_result", "content": "line 1\n</tool_result>"}, "Read")
        self.assertIn("### Tool result\n\n- tool: `Read`", out)
        self.assertIn("~~~~\nline 1\n</tool_result>\n~~~~", out)
        self.assertIn("real output of your own Read call", out)

    def test_an_error_is_marked_as_one(self):
        out = translate._tool_result_payload(
            {"type": "tool_result", "content": "boom", "is_error": True}, "Bash")
        self.assertIn("### Tool error", out)
        self.assertIn("That call failed", out)


class ToolOutputs(_Style):
    def test_zoom_carries_its_coordinates_as_a_list(self):
        with tempfile.TemporaryDirectory() as tree:
            path = os.path.join(tree, "doc.md")
            with open(path, "w") as handle:
                handle.write("# Heading\n" + "".join(f"line {n}\n" for n in range(1, 30)))
            out = run(tools.run_zoom(path, 5, None))
        self.assertIn("### Passage", out)
        self.assertIn(f"- file: `{path}`", out)
        self.assertIn("- complete: `true`", out)
        self.assertIn("     5  line 4", out)

    def test_inventory_is_a_table_and_a_list(self):
        with tempfile.TemporaryDirectory() as tree:
            os.makedirs(os.path.join(tree, "docs"))
            for name in ("a.py", "b.py", "docs/readme.md"):
                with open(os.path.join(tree, name), "w") as handle:
                    handle.write("# title\nbody\n")
            out = run(tools.run_inventory(tree, None))
        self.assertIn(f"## Inventory of `{tree}`", out)
        self.assertIn("3 files in 2 folders.", out)
        self.assertIn("| kind | files |", out)
        self.assertIn("- `docs/` (1 files)", out)
        self.assertEqual(_TAG.findall(out), [])

    def test_outline_render_still_yields_addressable_picks(self):
        with tempfile.TemporaryDirectory() as tree:
            path = os.path.join(tree, "spec.md")
            with open(path, "w") as handle:
                handle.write("# Intro\ntext\n## Security\nmore\n")
            toc = outline.render([path], read=_read)
        self.assertIn(f"- `{path}`", toc)
        self.assertRegex(toc, r"  - line 3, depth 2, \d+ tokens: Security")
        picks = outline.read_picks(f"{path}:3", [path])
        self.assertEqual(picks, [(path, 3)])


def _searcher(hit_word: str, answer: str):
    """A stub Agentaus that tells prompts apart by wording, not layout."""
    seen: list[str] = []

    async def call(prompt: str) -> str:
        seen.append(prompt)
        if "list the literal strings" in prompt.lower():
            return hit_word
        if "Decide whether the excerpt" in prompt:
            return answer if hit_word in prompt else "NONE"
        if "Three independent searches" in prompt:
            return "## Established\n- `gate.py:2`\n\n## Single-source\nnone"
        return "NONE"

    call.seen = seen  # type: ignore[attr-defined]
    return call


class SearchBehaviour(_Style):
    def test_a_search_reads_chunks_and_cites_the_hit(self):
        with tempfile.TemporaryDirectory() as tree:
            with open(os.path.join(tree, "gate.py"), "w") as handle:
                handle.write("import asyncio\n_limit = asyncio.Semaphore(6)\n")
            with open(os.path.join(tree, "other.py"), "w") as handle:
                handle.write("x = 1\n")
            call = _searcher("Semaphore", "2: _limit = asyncio.Semaphore(6)")
            out = run(tools.run_search("where do we cap calls", tree, None, call))
        chunks = [p for p in call.seen if "Decide whether the excerpt" in p]
        self.assertTrue(chunks)
        self.assertTrue(all("### Excerpt" in p and "<excerpt" not in p for p in chunks))
        self.assertIn("Semaphore(6)", out)
        self.assertIn("gate.py", out)

    def test_investigate_corroborates(self):
        with tempfile.TemporaryDirectory() as tree:
            with open(os.path.join(tree, "gate.py"), "w") as handle:
                handle.write("import asyncio\n_gate = asyncio.Semaphore(6)\n")
            call = _searcher("Semaphore", "2: _gate = asyncio.Semaphore(6)")
            out = run(tools.run_investigate("where is the cap", tree, call))
        self.assertIn("Established", out)
        merge = [p for p in call.seen if "Three independent searches" in p]
        self.assertTrue(merge and "### Reports" in merge[0])


class Compaction(_Style):
    def test_summaries_are_fenced_and_merged(self):
        prompts: list[str] = []

        async def call(text: str) -> str:
            prompts.append(text)
            if "Merge them into a single coherent record" in text:
                return "merged record"
            return "- a fact"

        compactor = compact.ConversationCompactor(call, verify=True, block=1)
        head = [{"role": "user", "content": f"message {n} " + "word " * 400} for n in range(6)]
        summary = run(compactor.summarise_head(head, chunk_budget=300))
        self.assertTrue(summary)
        firsts = [p for p in prompts if "You are compacting" in p]
        self.assertTrue(firsts and all("### Conversation" in p and "<conversation>" not in p
                                       for p in firsts))
        self.assertTrue(all("fenced block below" in p for p in firsts))
        gaps = [p for p in prompts if "missing from the SUMMARY" in p]
        self.assertTrue(gaps and "### Summary" in gaps[0] and "### Original" in gaps[0])
        merges = [p for p in prompts if "Merge them into a single coherent record" in p]
        self.assertTrue(merges and "### Summaries" in merges[0] and "<summaries>" not in merges[0])


if __name__ == "__main__":
    unittest.main()

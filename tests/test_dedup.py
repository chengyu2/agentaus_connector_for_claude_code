"""Two phrasings of one question should not run the search twice.

Observed live during a benchmark run: a turn searched `should_think`, got its answer,
then searched `should_think(` and ran the entire fan-out again - 29 seconds to rebuild a
result it was already holding. The repeat guard was there; it keyed on the arguments
verbatim, so a trailing bracket read as a different request.

The asymmetry that shapes this: a false miss costs one repeated search, a false hit
answers a question with a different question's result. So content words are kept and only
grammar is dropped.
"""

import unittest

from agentaus_bridge.server import _call_signature, _same_request, _singular


def search(query: str, path: str = "/repo") -> dict:
    return {"name": "agentaus_search", "arguments": {"query": query, "path": path}}


def same(one: str, two: str) -> bool:
    return _call_signature(search(one)) == _call_signature(search(two))


class OneQuestionAskedTwice(unittest.TestCase):
    def test_the_case_that_cost_a_real_turn_29_seconds(self):
        self.assertTrue(same("should_think", "should_think("))

    def test_case_does_not_make_it_a_new_request(self):
        self.assertTrue(same("should_think", "Should Think"))

    def test_rearranged_grammar_is_the_same_question(self):
        self.assertTrue(same("where is the gate", "the gate, where is it?"))

    def test_a_paraphrase_with_the_same_content_words_is_the_same_question(self):
        self.assertTrue(same("how does the bridge cap concurrency",
                             "what caps concurrency in the bridge"))

    def test_singular_and_plural_are_the_same_question(self):
        self.assertTrue(same("list the retries", "list retry"))
        self.assertTrue(same("the class hierarchy", "the classes hierarchy"))
        self.assertTrue(same("parse the boxes", "parse a box"))


class DifferentQuestionsStayDifferent(unittest.TestCase):
    """The direction that matters. A false hit returns the wrong result entirely."""

    def test_different_symbols_do_not_collide(self):
        self.assertFalse(same("should_think", "worth_reviewing_turn"))

    def test_same_verb_different_object_does_not_collide(self):
        self.assertFalse(same("cap concurrency", "cap retries"))

    def test_same_shape_different_subject_does_not_collide(self):
        self.assertFalse(same("how does search work", "how does compaction work"))

    def test_a_query_that_is_entirely_grammar_still_distinguishes_itself(self):
        """Stripping function words would leave both empty, so it is not applied."""
        self.assertFalse(same("where is it", "what is it"))

    def test_a_different_path_is_a_different_request(self):
        self.assertNotEqual(_call_signature(search("gate", "/a")),
                            _call_signature(search("gate", "/b")))

    def test_a_different_tool_is_a_different_request(self):
        self.assertNotEqual(
            _call_signature({"name": "agentaus_search", "arguments": {"query": "x"}}),
            _call_signature({"name": "agentaus_investigate", "arguments": {"query": "x"}}))


class Stemming(unittest.TestCase):
    def test_double_s_words_are_not_plurals(self):
        self.assertEqual(_singular("class"), "class")
        self.assertEqual(_singular("process"), "process")

    def test_short_words_are_left_alone(self):
        self.assertEqual(_singular("is"), "is")
        self.assertEqual(_singular("bus"), "bus")

    def test_regular_plurals_fold(self):
        self.assertEqual(_singular("caps"), "cap")
        self.assertEqual(_singular("retries"), "retry")
        self.assertEqual(_singular("classes"), "class")


class Robustness(unittest.TestCase):
    def test_non_text_arguments_are_left_alone(self):
        """Only free-text fields are normalised; a limit of 40 is not a phrasing."""
        one = _call_signature({"name": "Read", "arguments": {"file_path": "/a", "limit": 40}})
        two = _call_signature({"name": "Read", "arguments": {"file_path": "/a", "limit": 41}})
        self.assertNotEqual(one, two)

    def test_arguments_as_a_json_string_still_key(self):
        encoded = _call_signature({"name": "agentaus_search",
                                   "arguments": '{"query": "should_think(", "path": "/repo"}'})
        self.assertEqual(encoded, _call_signature(search("should_think")))

    def test_unparseable_arguments_do_not_raise(self):
        self.assertIsInstance(
            _call_signature({"name": "x", "arguments": "not json"}), str)

    def test_empty_and_missing_are_handled(self):
        self.assertEqual(_same_request(""), "")
        self.assertEqual(_same_request(None), "")
        self.assertIsInstance(_call_signature({}), str)


if __name__ == "__main__":
    unittest.main()

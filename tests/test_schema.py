"""Tool arguments checked against the schema the client published for that tool.

The case that prompted this: Agentaus called `AskUserQuestion` offering a single option
to choose between. Claude Code rejected it with Zod internals, the turn was spent, and
the bridge - which had the schema in hand the whole time - had not looked.
"""

import unittest

from agentaus_bridge import schema


ASK_USER_QUESTION = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "required": ["question", "header", "options", "multiSelect"],
                "properties": {
                    "question": {"type": "string"},
                    "header": {"type": "string", "maxLength": 12},
                    "multiSelect": {"type": "boolean"},
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "required": ["label", "description"],
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }
    },
}

READ = {
    "type": "object",
    "required": ["file_path"],
    "properties": {
        "file_path": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
        "offset": {"type": "integer", "minimum": 0},
    },
}


def one_option():
    return {
        "questions": [
            {
                "question": "Which approach?",
                "header": "Approach",
                "multiSelect": False,
                "options": [{"label": "Only one", "description": "d"}],
            }
        ]
    }


class TheReportedFailure(unittest.TestCase):
    def test_a_question_with_one_option_is_caught_before_the_client_sees_it(self):
        _, problems = schema.validate(one_option(), ASK_USER_QUESTION)
        self.assertEqual(len(problems), 1)
        self.assertIn("at least 2", problems[0])
        self.assertIn("options", problems[0])

    def test_the_correction_names_the_tool_and_says_what_to_do(self):
        _, problems = schema.validate(one_option(), ASK_USER_QUESTION)
        message = schema.correction_for("AskUserQuestion", problems, ASK_USER_QUESTION)
        self.assertIn("AskUserQuestion", message)
        self.assertIn("at least 2", message)
        self.assertNotIn("too_small", message, "must not echo the client's validator")

    def test_a_well_formed_call_passes_untouched(self):
        good = one_option()
        good["questions"][0]["options"].append({"label": "Two", "description": "d"})
        fixed, problems = schema.validate(good, ASK_USER_QUESTION)
        self.assertEqual(problems, [])
        self.assertEqual(fixed, good)


class SilentRepair(unittest.TestCase):
    """Slips with only one possible meaning are fixed rather than round-tripped.

    Same principle that already resolves `read` to `Read`: a model that double-encoded
    its JSON has not made a decision worth asking about.
    """

    def test_double_encoded_json_is_decoded(self):
        fixed, problems = schema.validate(
            {"questions": '[{"question":"q","header":"h","multiSelect":false,'
                          '"options":[{"label":"a","description":"d"},'
                          '{"label":"b","description":"d"}]}]'},
            ASK_USER_QUESTION,
        )
        self.assertEqual(problems, [])
        self.assertIsInstance(fixed["questions"], list)

    def test_a_lone_item_is_wrapped_when_a_list_was_wanted(self):
        fixed, _ = schema.validate({"questions": {"question": "q"}}, ASK_USER_QUESTION)
        self.assertIsInstance(fixed["questions"], list)

    def test_stringy_booleans_and_numbers_are_converted(self):
        fixed, problems = schema.validate(
            {"file_path": "/a/b.py", "limit": "40"}, READ)
        self.assertEqual(fixed["limit"], 40)
        self.assertEqual(problems, [])

    def test_a_numeric_string_stays_a_string_where_a_string_was_wanted(self):
        fixed, _ = schema.validate({"file_path": "42"}, READ)
        self.assertEqual(fixed["file_path"], "42")

    def test_arguments_arriving_as_a_json_string_are_parsed(self):
        fixed, problems = schema.validate('{"file_path": "/a/b.py"}', READ)
        self.assertEqual(fixed["file_path"], "/a/b.py")
        self.assertEqual(problems, [])

    def test_arguments_that_are_not_json_at_all_are_reported_not_raised(self):
        _, problems = schema.validate("not json", READ)
        self.assertTrue(problems)
        self.assertIn("JSON", problems[0])


class Constraints(unittest.TestCase):
    def test_a_missing_required_field_is_named(self):
        _, problems = schema.validate({"limit": 5}, READ)
        self.assertTrue(any("file_path" in p for p in problems))

    def test_a_wrong_type_is_reported_with_what_was_actually_sent(self):
        _, problems = schema.validate({"file_path": ["/a"], "limit": 1}, READ)
        self.assertTrue(any("must be string" in p for p in problems))

    def test_a_boolean_is_not_accepted_as_a_number(self):
        _, problems = schema.validate({"file_path": "/a", "limit": True}, READ)
        self.assertTrue(problems)

    def test_a_value_below_the_minimum_is_caught(self):
        _, problems = schema.validate({"file_path": "/a", "limit": 0}, READ)
        self.assertTrue(any("at least 1" in p for p in problems))

    def test_an_over_long_string_reports_its_actual_length(self):
        call = one_option()
        call["questions"][0]["header"] = "far too long to be a header"
        call["questions"][0]["options"].append({"label": "b", "description": "d"})
        _, problems = schema.validate(call, ASK_USER_QUESTION)
        self.assertTrue(any("at most 12 characters" in p for p in problems))

    def test_an_enum_lists_what_was_allowed(self):
        enum = {"type": "object", "properties": {"mode": {"enum": ["fast", "slow"]}}}
        _, problems = schema.validate({"mode": "medium"}, enum)
        self.assertTrue(any('"fast"' in p for p in problems))

    def test_nested_faults_are_addressed_by_path(self):
        call = one_option()
        call["questions"][0]["options"] = [{"label": "a"}, {"description": "d"}]
        _, problems = schema.validate(call, ASK_USER_QUESTION)
        self.assertTrue(any("questions[0].options[0]" in p for p in problems))
        self.assertTrue(any("questions[0].options[1]" in p for p in problems))


class NoSchema(unittest.TestCase):
    def test_a_tool_with_no_published_schema_is_left_alone(self):
        """Absence of a schema is not licence to reject the call."""
        fixed, problems = schema.validate({"anything": 1}, {})
        self.assertEqual(problems, [])
        self.assertEqual(fixed, {"anything": 1})

    def test_unknown_keywords_do_not_cause_false_rejections(self):
        exotic = {"type": "object", "patternProperties": {"^x": {}},
                  "properties": {"a": {"type": "string"}}}
        _, problems = schema.validate({"a": "fine", "xyz": 1}, exotic)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()

"""The run_tools dispatcher: several tool calls in one Agentaus reply. See batching.py."""

import json
import unittest

from agentaus_bridge import batching


def _tool(name, props):
    return {"type": "function", "function": {
        "name": name, "description": f"{name} things.",
        "parameters": {"type": "object", "properties": props}}}


TOOLS = [_tool("Read", {"file_path": {"type": "string"}}),
         _tool("Grep", {"pattern": {"type": "string"}})]


class WrapPayload(unittest.TestCase):
    def test_run_tools_is_added_beside_the_real_tools(self):
        wrapped = batching.wrap_payload({"messages": [{"role": "system", "content": "S"}],
                                         "tools": TOOLS, "tool_choice": "auto"})
        self.assertEqual([t["function"]["name"] for t in wrapped["tools"]],
                         ["Read", "Grep", "run_tools"])
        enum = (wrapped["tools"][2]["function"]["parameters"]["properties"]["tool_uses"]
                ["items"]["properties"]["recipient_name"]["enum"])
        self.assertEqual(enum, ["Read", "Grep"])
        self.assertEqual(wrapped["tool_choice"], "auto")

    def test_the_note_joins_the_system_prompt(self):
        wrapped = batching.wrap_payload({"messages": [{"role": "system", "content": "S"},
                                                      {"role": "user", "content": "u"}],
                                         "tools": TOOLS})
        self.assertTrue(wrapped["messages"][0]["content"].startswith("S\n\n"))
        self.assertIn("run_tools", wrapped["messages"][0]["content"])
        self.assertEqual(len(wrapped["messages"]), 2)

    def test_the_note_is_added_when_there_is_no_system_prompt(self):
        wrapped = batching.wrap_payload({"messages": [{"role": "user", "content": "u"}],
                                         "tools": TOOLS})
        self.assertEqual(wrapped["messages"][0]["role"], "system")

    def test_already_wrapped_is_left_alone(self):
        once = batching.wrap_payload({"messages": [], "tools": TOOLS})
        self.assertIs(batching.wrap_payload(once), once)

    def test_no_tools_is_a_no_op(self):
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertIs(batching.wrap_payload(payload), payload)

    def test_does_not_mutate_the_kept_payload(self):
        payload = {"tools": TOOLS, "messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "type": "function",
                 "function": {"name": "Read", "arguments": '{"file_path": "x"}'}}]},
            {"role": "tool", "tool_call_id": "a", "content": "X"}]}
        before = json.dumps(payload)
        batching.wrap_payload(payload)
        self.assertEqual(json.dumps(payload), before)

    def test_history_collapses_to_one_call_and_one_result(self):
        messages = [
            {"role": "user", "content": "read both"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c-u0", "type": "function",
                 "function": {"name": "Read", "arguments": '{"file_path": "a.py"}'}},
                {"id": "c-u1", "type": "function",
                 "function": {"name": "Read", "arguments": '{"file_path": "b.py"}'}}]},
            {"role": "tool", "tool_call_id": "c-u0", "content": "AAA"},
            {"role": "tool", "tool_call_id": "c-u1", "content": "BBB"},
            {"role": "user", "content": "thanks"},
        ]
        out = batching.wrap_payload({"tools": TOOLS, "messages": messages})["messages"]
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool", "user"])
        out = out[1:]
        call = out[1]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "run_tools")
        self.assertEqual(call["id"], "c-u0")
        uses = json.loads(call["function"]["arguments"])["tool_uses"]
        self.assertEqual([u["parameters"]["file_path"] for u in uses], ["a.py", "b.py"])
        self.assertEqual(out[2]["tool_call_id"], "c-u0")
        self.assertLess(out[2]["content"].index("AAA"), out[2]["content"].index("BBB"))
        self.assertIn("tool_uses[1]: Read", out[2]["content"])

    def test_single_call_stays_native(self):
        messages = [
            {"role": "assistant", "content": "looking", "tool_calls": [
                {"id": "x", "type": "function",
                 "function": {"name": "Grep", "arguments": '{"pattern": "foo"}'}}]},
            {"role": "tool", "tool_call_id": "x", "content": "hit"},
        ]
        out = batching.wrap_payload({"tools": TOOLS, "messages": messages})["messages"][1:]
        self.assertEqual(out, messages)

    def test_missing_result_is_named(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "p", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
                {"id": "q", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "p", "content": "one"},
        ]
        out = batching.wrap_payload({"tools": TOOLS, "messages": messages})["messages"][1:]
        self.assertIn("not run", out[1]["content"])


class Expand(unittest.TestCase):
    def test_dispatcher_call_becomes_real_calls_with_unique_ids(self):
        calls = [{"id": "k", "name": "run_tools", "arguments": json.dumps({"tool_uses": [
            {"recipient_name": "Read", "parameters": {"file_path": "a"}},
            {"recipient_name": "functions.Grep", "parameters": {"pattern": "z"}}]})}]
        out = batching.expand_calls(calls)
        self.assertEqual([c["name"] for c in out], ["Read", "Grep"])
        self.assertEqual([c["id"] for c in out], ["k-u0", "k-u1"])
        self.assertEqual(json.loads(out[0]["arguments"]), {"file_path": "a"})

    def test_single_use_keeps_the_call_id(self):
        calls = [{"id": "k", "name": "run_tools", "arguments": json.dumps({"tool_uses": [
            {"recipient_name": "Read", "parameters": {"file_path": "a"}}]})}]
        self.assertEqual(batching.expand_calls(calls)[0]["id"], "k")

    def test_tolerates_other_spellings(self):
        calls = [{"id": "k", "name": "run_tools", "arguments": json.dumps({"calls": [
            {"tool": "Read", "input": {"file_path": "a"}},
            {"name": "Grep", "pattern": "z"}]})}]
        out = batching.expand_calls(calls)
        self.assertEqual([c["name"] for c in out], ["Read", "Grep"])
        self.assertEqual(json.loads(out[1]["arguments"]), {"pattern": "z"})

    def test_unreadable_dispatcher_call_is_left_alone(self):
        calls = [{"id": "k", "name": "run_tools", "arguments": "{not json"}]
        self.assertEqual(batching.expand_calls(calls), calls)

    def test_other_calls_pass_through(self):
        calls = [{"id": "k", "name": "Read", "arguments": "{}"}]
        self.assertEqual(batching.expand_calls(calls), calls)

    def test_expand_response(self):
        data = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "k", "type": "function", "function": {"name": "run_tools", "arguments":
             json.dumps({"tool_uses": [{"recipient_name": "Read", "parameters": {}},
                                       {"recipient_name": "Grep", "parameters": {}}]})}}]},
            "finish_reason": "tool_calls"}]}
        out = batching.expand_response(data)
        names = [c["function"]["name"] for c in out["choices"][0]["message"]["tool_calls"]]
        self.assertEqual(names, ["Read", "Grep"])

    def test_round_trip_is_consistent(self):
        """What the model asked for is what it sees it asked for on the next request."""
        asked = {"tool_uses": [{"recipient_name": "Read", "parameters": {"file_path": "a"}},
                               {"recipient_name": "Grep", "parameters": {"pattern": "b"}}]}
        calls = batching.expand_calls(
            [{"id": "z", "name": "run_tools", "arguments": json.dumps(asked)}])
        history = [{"role": "assistant", "content": None, "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}]
        history += [{"role": "tool", "tool_call_id": c["id"], "content": "r"} for c in calls]
        out = batching.wrap_payload({"tools": TOOLS, "messages": history})["messages"][1:]
        self.assertEqual(json.loads(out[0]["tool_calls"][0]["function"]["arguments"]), asked)

    def test_id_less_calls_expand_to_different_ids_each_time(self):
        call = {"name": "run_tools", "arguments": json.dumps({"tool_uses": [
            {"recipient_name": "Read", "parameters": {}},
            {"recipient_name": "Grep", "parameters": {}}]})}
        first = [c["id"] for c in batching.expand_calls([dict(call)])]
        second = [c["id"] for c in batching.expand_calls([dict(call)])]
        self.assertEqual(len(set(first + second)), 4)

    def test_tool_uses_sent_as_a_json_string(self):
        uses = [{"recipient_name": "Read", "parameters": {"file_path": "a"}},
                {"recipient_name": "Grep", "parameters": {"pattern": "b"}}]
        calls = [{"id": "k", "name": "run_tools",
                  "arguments": json.dumps({"tool_uses": json.dumps(uses)})}]
        self.assertEqual([c["name"] for c in batching.expand_calls(calls)], ["Read", "Grep"])

    def test_both_paths_accept_the_same_spellings(self):
        args = json.dumps({"tool_uses": [{"recipient_name": "Read", "parameters": {}},
                                         {"recipient_name": "Grep", "parameters": {}}]})
        for name in ("run_tools", "functions.run_tools", "run-tools", "Run_Tools"):
            streamed = batching.expand_calls([{"id": "k", "name": name, "arguments": args}])
            self.assertEqual([c["name"] for c in streamed], ["Read", "Grep"], name)
            data = {"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "k", "type": "function",
                 "function": {"name": name, "arguments": args}}]}}]}
            buffered = batching.expand_response(data)["choices"][0]["message"]["tool_calls"]
            self.assertEqual([c["function"]["name"] for c in buffered], ["Read", "Grep"], name)
        self.assertFalse(batching.is_dispatch("Read"))


class Corrections(unittest.TestCase):
    def test_unexpandable_run_tools_gets_the_usage_not_invented(self):
        for args in ({"tool_uses": []}, {}, "{not json"):
            raw = args if isinstance(args, str) else json.dumps(args)
            [call] = batching.expand_calls([{"id": "k", "name": "run_tools", "arguments": raw}])
            self.assertEqual(batching.correction_for(call), batching.USAGE, args)
        self.assertIn("non-empty list of {recipient_name, parameters}", batching.USAGE)

    def test_nested_run_tools_gets_the_usage(self):
        calls = batching.expand_calls([{"id": "k", "name": "run_tools", "arguments":
                                        json.dumps({"tool_uses": [
            {"recipient_name": "Read", "parameters": {}},
            {"recipient_name": "functions.run_tools", "parameters": {"tool_uses": []}}]})}])
        self.assertIsNone(batching.correction_for(calls[0]))
        self.assertEqual(batching.correction_for(calls[1]), batching.USAGE)

    def test_valid_siblings_are_answered_not_run(self):
        good = {"id": "k-u0", "name": "Read", "arguments": "{}"}
        bad = {"id": "k-u1", "name": "open_file", "arguments": "{}"}
        renamed = {**good, "name": "Read", "asked_as": "read"}   # the partition's copy
        calls, results = batching.answer_batch([good, bad], [(bad, "no such tool")])
        self.assertEqual([c["id"] for c in calls], ["k-u1", "k-u0"])
        self.assertEqual(results, ["no such tool", batching.NOT_RUN])
        calls, _ = batching.answer_batch([renamed, bad], [(good, "bad args")])
        self.assertEqual([c["id"] for c in calls], ["k-u0", "k-u1"])

    def test_not_run_siblings_survive_the_collapse(self):
        good = {"id": "k-u0", "name": "Read", "arguments": '{"file_path": "a"}'}
        bad = {"id": "k-u1", "name": "open_file", "arguments": "{}"}
        calls, results = batching.answer_batch([good, bad], [(bad, "no such tool")])
        history = [{"role": "assistant", "content": None, "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}]
        history += [{"role": "tool", "tool_call_id": c["id"], "content": r}
                    for c, r in zip(calls, results)]
        out = batching.wrap_payload({"tools": TOOLS, "messages": history})["messages"][1:]
        self.assertIn("no such tool", out[1]["content"])
        self.assertIn("not run: another call in the same reply", out[1]["content"])


class CollapseAcrossMessages(unittest.TestCase):
    @staticmethod
    def _batch(ids, results):
        return [{"role": "assistant", "content": None, "tool_calls": [
            {"id": i, "type": "function", "function": {"name": "Read", "arguments": "{}"}}
            for i in ids]}] + [{"role": "tool", "tool_call_id": i, "content": r}
                               for i, r in zip(ids, results)]

    def test_reused_ids_stay_with_their_own_message(self):
        """harmony.py numbers recovered calls from harmony_0 in every response."""
        ids = ["harmony_0", "harmony_1"]
        messages = self._batch(ids, ["A0", "A1"]) + self._batch(ids, ["B0", "B1"])
        out = batching.wrap_payload({"tools": TOOLS, "messages": messages})["messages"][1:]
        self.assertEqual([m["role"] for m in out], ["assistant", "tool", "assistant", "tool"])
        self.assertIn("A0", out[1]["content"])
        self.assertIn("A1", out[1]["content"])
        self.assertNotIn("B0", out[1]["content"])
        self.assertIn("B0", out[3]["content"])
        self.assertIn("B1", out[3]["content"])

    def test_reused_id_after_a_single_call_is_not_folded_back(self):
        messages = self._batch(["c0", "c1"], ["A0", "A1"]) + self._batch(["c0"], ["S"])
        out = batching.wrap_payload({"tools": TOOLS, "messages": messages})["messages"][1:]
        self.assertEqual(out[3], {"role": "tool", "tool_call_id": "c0", "content": "S"})
        self.assertNotIn("S", out[1]["content"])


if __name__ == "__main__":
    unittest.main()

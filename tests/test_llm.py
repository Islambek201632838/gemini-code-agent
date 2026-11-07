"""Сериализация сообщений истории (основа --resume): thought signatures обязаны пережить круг туда-обратно."""
import json
import sys
import unittest
from pathlib import Path

from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm import LLM  # noqa: E402


class TestMessageRoundTrip(unittest.TestCase):
    def roundtrip(self, msg):
        return LLM.load_message(json.loads(json.dumps(LLM.dump_message(msg), ensure_ascii=False)))

    def test_model_message_with_signatures_is_identical(self):
        msg = types.Content(role="model", parts=[
            types.Part(text="Читаю файл", thought_signature=b"\x00\xff\x10sig"),
            types.Part(function_call=types.FunctionCall(name="read_file", args={"path": "a.py"}),
                       thought_signature=b"\xde\xad"),
        ])
        self.assertEqual(self.roundtrip(msg), msg)

    def test_tool_results_message_with_cyrillic(self):
        msg = types.Content(role="user", parts=[types.Part.from_function_response(
            name="run_bash", response={"ok": True, "output": "привет"})])
        self.assertEqual(self.roundtrip(msg), msg)

    def test_unanswered_calls_only_when_last_is_model_with_calls(self):
        call = types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name="glob", args={"pattern": "*"}))])
        user = types.Content(role="user", parts=[types.Part(text="hi")])
        self.assertEqual([c.name for c in LLM.unanswered_calls([user, call])], ["glob"])
        self.assertEqual(LLM.unanswered_calls([call, user]), [])
        self.assertEqual(LLM.unanswered_calls([]), [])


if __name__ == "__main__":
    unittest.main()

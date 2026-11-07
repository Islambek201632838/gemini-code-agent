"""Тесты цикла агента на фейковой модели: Ctrl+C, защита от зацикливания, возобновление сессии.

Главное, что проверяется: история остаётся валидной (на каждый вызов есть результат, роли чередуются).
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent as agent_mod  # noqa: E402
from llm import Reply, ToolCall  # noqa: E402


class FakeLLM:
    """Сообщения истории — обычные dict, как «родные» объекты провайдера для агента непрозрачны."""
    model = "fake"

    def __init__(self, replies):
        self.replies = list(replies)  # элементы: Reply или исключение, которое надо бросить

    def user_message(self, text):
        return {"role": "user", "text": text}

    def tool_results_message(self, results):
        return {"role": "user", "results": [[c.name, r] for c, r in results]}

    def model_message(self, text):
        return {"role": "model", "text": text}

    def dump_message(self, m):
        return json.loads(json.dumps(m))

    def load_message(self, d):
        return d

    def unanswered_calls(self, history):
        if history and history[-1]["role"] == "model" and history[-1].get("calls"):
            return [ToolCall(n, a) for n, a in history[-1]["calls"]]
        return []

    def generate(self, history, system, schemas):
        r = self.replies.pop(0) if self.replies else Reply(text="done", message={"role": "model", "text": "done"})
        if isinstance(r, BaseException):
            raise r
        return r


def call_reply(name="read_file", text="", **args):
    return Reply(text=text, tool_calls=[ToolCall(name, args)],
                 message={"role": "model", "text": text, "calls": [[name, args]]})


def text_reply(text):
    return Reply(text=text, message={"role": "model", "text": text})


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.ws = self.dir / "ws"
        self.ws.mkdir()
        (self.ws / "a.txt").write_text("hello")
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

    def make(self, replies, **kw):
        return agent_mod.Agent(FakeLLM(replies), self.ws, auto_yes=True, log_dir=self.dir / "logs", **kw)

    def assert_valid_history(self, history):
        """На каждый вызов модели есть результат в следующем сообщении; две реплики одной роли подряд не допускаются."""
        for prev, nxt in zip(history, history[1:]):
            self.assertNotEqual(prev["role"], nxt["role"], history)
            if prev.get("calls"):
                self.assertEqual([n for n, _ in prev["calls"]], [n for n, _ in nxt["results"]])


class TestCancel(AgentTestCase):
    def test_ctrl_c_during_tool_answers_every_call(self):
        reply = Reply(text="", tool_calls=[ToolCall("read_file", {"path": "a.txt"}),
                                           ToolCall("run_bash", {"command": "sleep 99"})],
                      message={"role": "model", "calls": [["read_file", {"path": "a.txt"}],
                                                          ["run_bash", {"command": "sleep 99"}]]})
        a = self.make([reply])
        real = agent_mod.tools.execute

        def execute(ws, name, args, **kw):
            if name == "run_bash":
                raise KeyboardInterrupt
            return real(ws, name, args, **kw)

        with mock.patch.object(agent_mod.tools, "execute", execute):
            msg = a.run("go")
        self.assertIn("Прервано", msg)
        self.assert_valid_history(a.history)
        results = dict(a.history[-2]["results"])
        self.assertTrue(results["read_file"]["ok"])                  # уже выполненный результат сохранён
        self.assertEqual(results["run_bash"]["error"], "cancelled by user")
        self.assertEqual(a.history[-1]["role"], "model")
        self.assertEqual(self.make([text_reply("ok")]).run("next"), "ok")  # агент пригоден дальше

    def test_ctrl_c_during_llm_call_keeps_roles_alternating(self):
        a = self.make([KeyboardInterrupt()])
        a.run("go")
        self.assertEqual([m["role"] for m in a.history], ["user", "model"])
        a.llm.replies = [text_reply("fine")]
        self.assertEqual(a.run("again"), "fine")
        self.assert_valid_history(a.history)

    def test_ctrl_c_in_confirmation_prompt(self):
        a = self.make([call_reply("write_file", path="x", content="y")])
        a.auto_yes = False
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            a.run("go")
        self.assert_valid_history(a.history)
        self.assertFalse((self.ws / "x").exists())


class TestLoopGuard(AgentTestCase):
    def test_identical_calls_are_blocked_then_stopped(self):
        a = self.make([call_reply(path="a.txt") for _ in range(10)], max_steps=20)
        msg = a.run("go")
        self.assertIn("зацикливание", msg)
        self.assert_valid_history(a.history)
        results = [r for m in a.history if "results" in m for _, r in m["results"]]
        self.assertTrue(results[0]["ok"] and results[1]["ok"])        # первые два выполнены
        self.assertIn("NOT run again", results[2]["error"])           # третий заблокирован с подсказкой
        self.assertIn("NOT run again", results[3]["error"])
        self.assertEqual(results[4]["error"], "stopped: repeated identical call")
        self.assertEqual(len(a.llm.replies), 5)                       # пятый вызов остановил цикл

    def test_different_calls_in_between_reset_the_counter(self):
        seq = [call_reply(path="a.txt"), call_reply(path="a.txt"), call_reply("glob", pattern="*"),
               call_reply(path="a.txt"), call_reply(path="a.txt"), text_reply("done")]
        self.assertEqual(self.make(seq).run("go"), "done")


class TestResume(AgentTestCase):
    def test_history_is_restored_and_log_continues(self):
        a = self.make([call_reply(path="a.txt", text="Читаю a.txt"), text_reply("готово")])
        a.run("задача")
        b = self.make([text_reply("продолжаю")])
        n = b.resume(a.log_path)
        self.assertEqual(b.history, a.history)
        self.assertEqual(n, len(a.history))
        self.assertEqual(b.log_path, a.log_path)
        b.run("ещё")
        self.assertEqual(len(a.log_path.read_text(encoding="utf-8").splitlines()) > n, True)
        self.assertEqual(agent_mod.find_latest_log(self.dir / "logs"), a.log_path)

    def test_dangling_tool_call_is_closed_on_resume(self):
        a = self.make([call_reply(path="a.txt")])
        a.history.append(a.llm.user_message("x"))  # имитация: лог оборвался на вызове без результата
        a._add(call_reply(path="a.txt").message)
        b = self.make([])
        b.resume(a.log_path)
        self.assert_valid_history(b.history)
        self.assertEqual(b.history[-2]["results"][0][1]["error"], "session was interrupted before this call ran")

    def test_old_format_log_is_refused(self):
        old = self.dir / "logs"
        old.mkdir()
        (old / "run-1.jsonl").write_text(json.dumps({"event": "task", "task": "x"}) + "\n", encoding="utf-8")
        self.assertIsNone(agent_mod.find_latest_log(old))
        with self.assertRaises(ValueError):
            self.make([]).resume(old / "run-1.jsonl")


class TestExplanationText(AgentTestCase):
    def test_prompt_requires_text_before_tool_calls(self):
        self.assertIn("Before every tool call", agent_mod.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()

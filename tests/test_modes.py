"""Тесты режима планирования (/plan), автосжатия и субагентов (run_subagent).

Фейковая модель общая у родителя и субагента: ответы берутся из одной очереди по порядку обращений.
"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import commands  # noqa: E402
import project  # noqa: E402
import tools  # noqa: E402
from llm import Reply  # noqa: E402
from tests.test_agent import AgentTestCase, FakeLLM, call_reply, text_reply  # noqa: E402


class RecordingLLM(FakeLLM):
    """Запоминает, с чем вызвали generate, и умеет summarize."""

    def __init__(self, replies, summary="SUMMARY"):
        super().__init__(replies)
        self.calls, self.summary, self.summarized = [], summary, 0

    def generate(self, history, system, schemas):
        self.calls.append({"system": system, "tools": [s["name"] for s in schemas], "history": list(history)})
        return super().generate(history, system, schemas)

    def summarize(self, history, instructions=""):
        self.summarized += 1
        if isinstance(self.summary, BaseException):
            raise self.summary
        return self.summary


class ModeTestCase(AgentTestCase):
    def make(self, replies, **kw):
        import agent as agent_mod
        self.llm = RecordingLLM(replies)
        return agent_mod.Agent(self.llm, self.ws, auto_yes=True, log_dir=self.dir / "logs", **kw)


class TestPlanMode(ModeTestCase):
    def test_only_read_tools_and_suffix_in_plan_mode(self):
        a = self.make([text_reply("plan")])
        a.set_plan_mode(True)
        a.run("task")
        sent = self.llm.calls[0]
        self.assertEqual(set(sent["tools"]), tools.READ_ONLY_TOOLS)
        self.assertIn("PLAN MODE", sent["system"])

    def test_normal_mode_after_plan_off(self):
        a = self.make([text_reply("plan"), text_reply("ok")])
        a.set_plan_mode(True)
        a.run("t1")
        a.set_plan_mode(False)
        a.run("t2")
        self.assertIn("write_file", self.llm.calls[1]["tools"])
        self.assertNotIn("PLAN MODE", self.llm.calls[1]["system"])
        self.assert_valid_history(a.history)

    def test_forbidden_call_is_rejected_as_result(self):
        a = self.make([call_reply("write_file", path="x.txt", content="no"), text_reply("plan")])
        a.set_plan_mode(True)
        a.run("task")
        self.assertFalse((self.ws / "x.txt").exists())
        self.assertIn("plan mode", a.history[2]["results"][0][1]["error"])
        self.assert_valid_history(a.history)

    def test_read_call_still_works(self):
        a = self.make([call_reply("read_file", path="a.txt"), text_reply("plan")])
        a.set_plan_mode(True)
        a.run("task")
        self.assertTrue(a.history[2]["results"][0][1]["ok"])

    def test_plan_command(self):
        a = self.make([])
        self.assertTrue(commands.handle(a, "/plan"))
        self.assertTrue(a.plan_mode)
        commands.handle(a, "/plan off")
        self.assertFalse(a.plan_mode)
        commands.handle(a, "/plan nonsense")
        self.assertFalse(a.plan_mode)

    def test_plan_name_is_reserved(self):
        self.assertIn("plan", project.RESERVED)


class TestAutoCompact(ModeTestCase):
    def reply_with_prompt(self, tokens):
        return Reply(text="ok", usage={"prompt": tokens}, message={"role": "model", "text": "ok"})

    def test_compacts_before_next_task_when_over_threshold(self):
        a = self.make([self.reply_with_prompt(5000), text_reply("second")], compact_at=1000)
        a.run("first")
        self.assertEqual(self.llm.summarized, 0)  # внутри задачи не сжимаем
        a.run("second")
        self.assertEqual(self.llm.summarized, 1)
        self.assertIn("SUMMARY", a.history[0]["text"])
        self.assertEqual(a.history[-2]["text"], "second")
        self.assert_valid_history(a.history)

    def test_no_compact_below_threshold_or_without_usage_or_disabled(self):
        for replies, kw in ([self.reply_with_prompt(10), {"compact_at": 1000}],
                            [text_reply("ok"), {"compact_at": 1000}],          # usage нет
                            [self.reply_with_prompt(5000), {}]):               # порога нет
            a = self.make([replies, text_reply("next")], **kw)
            a.run("first")
            a.run("second")
            self.assertEqual(self.llm.summarized, 0)

    def test_summarize_failure_does_not_break_task(self):
        a = self.make([self.reply_with_prompt(5000), text_reply("second")], compact_at=1000)
        a.run("first")
        self.llm.summary = RuntimeError("api down")
        self.assertEqual(a.run("second"), "second")
        self.assert_valid_history(a.history)
        self.assertEqual(len(a.history), 4)  # история сохранена, задача выполнена

    def test_last_prompt_reset_after_compact(self):
        a = self.make([self.reply_with_prompt(5000), text_reply("second")], compact_at=1000)
        a.run("first")
        a.run("second")
        self.assertEqual(self.llm.summarized, 1)
        # после успешного сжатия и ответа без usage порог не срабатывает снова
        a.run("third")
        self.assertEqual(self.llm.summarized, 1)


class TestSubagent(ModeTestCase):
    def test_parent_gets_only_summary(self):
        a = self.make([
            call_reply("run_subagent", prompt="find a.txt"),   # родитель
            call_reply("read_file", path="a.txt"),             # субагент
            text_reply("a.txt says hello"),                    # субагент: итог
            text_reply("final"),                               # родитель
        ])
        self.assertEqual(a.run("task"), "final")
        result = a.history[2]["results"][0][1]
        self.assertEqual(result, {"ok": True, "status": "done", "id": 1, "result": "a.txt says hello"})
        self.assertEqual(len(a.history), 4)                    # внутренности субагента в историю не попали
        self.assert_valid_history(a.history)
        sub_call = self.llm.calls[1]                           # первый запрос субагента
        self.assertEqual(sub_call["history"][0]["text"], "find a.txt")  # чистая история
        self.assertEqual(set(sub_call["tools"]), tools.READ_ONLY_TOOLS)  # без записи и без рекурсии
        self.assertTrue(list((self.dir / "logs").glob("run-*-subagents/sub01.jsonl")))

    def test_subagent_cannot_write(self):
        a = self.make([
            call_reply("run_subagent", prompt="try"),
            call_reply("write_file", path="x.txt", content="no"),
            text_reply("blocked"),
            text_reply("final"),
        ])
        a.run("task")
        self.assertFalse((self.ws / "x.txt").exists())

    def test_subagent_failure_becomes_error_result(self):
        a = self.make([call_reply("run_subagent", prompt="x"), RuntimeError("boom"), text_reply("final")])
        a.run("task")
        result = a.history[2]["results"][0][1]
        self.assertFalse(result["ok"])
        self.assertIn("did not finish", result["error"])
        self.assert_valid_history(a.history)

    def test_empty_prompt_is_error(self):
        a = self.make([call_reply("run_subagent", prompt="  "), text_reply("final")])
        a.run("task")
        self.assertFalse(a.history[2]["results"][0][1]["ok"])

    def test_ctrl_c_in_subagent_stops_parent(self):
        a = self.make([call_reply("run_subagent", prompt="x"), KeyboardInterrupt()])
        out = a.run("task")
        self.assertIn("Прервано", out)
        self.assert_valid_history(a.history)

    def test_schema_registered_but_not_in_tools(self):
        self.assertIn("run_subagent", [s["name"] for s in tools.SCHEMAS])
        self.assertNotIn("run_subagent", tools.TOOLS)

    def test_plan_mode_hides_subagent_tool(self):
        a = self.make([text_reply("plan")])
        a.set_plan_mode(True)
        a.run("task")
        self.assertNotIn("run_subagent", self.llm.calls[0]["tools"])


def agent_mod_limits():
    import agent as m
    return m.SUBAGENT_MAX_STEPS, min(25, m.SUBAGENT_WRITE_MAX_STEPS)


class SplitLLM(RecordingLLM):
    """Ответы родителю и субагенту из разных очередей (в фоне порядок обращений не детерминирован)."""

    def __init__(self, parent, sub):
        super().__init__([])
        self.parent_q, self.sub_q, self.lock = list(parent), list(sub), threading.Lock()

    def generate(self, history, system, schemas):
        with self.lock:
            self.replies = self.sub_q if "subagent" in system else self.parent_q
            return super().generate(history, system, schemas)


class TestSubagentExtras(ModeTestCase):
    def make_split(self, parent, sub, **kw):
        import agent as agent_mod
        self.llm = SplitLLM(parent, sub)
        return agent_mod.Agent(self.llm, self.ws, auto_yes=kw.pop("auto_yes", True), log_dir=self.dir / "logs", **kw)

    def test_write_subagent_can_write_but_not_bash(self):
        a = self.make_split(
            [call_reply("run_subagent", prompt="make x", write=True), text_reply("final")],
            [call_reply("write_file", path="x.txt", content="hi"), text_reply("wrote x.txt")])
        a.run("task")
        self.assertEqual((self.ws / "x.txt").read_text(), "hi")
        sub_tools = set(self.llm.calls[1]["tools"])
        self.assertEqual(sub_tools, tools.READ_ONLY_TOOLS | {"write_file", "edit_file"})
        self.assertNotIn("run_bash", sub_tools)

    def test_write_subagent_reports_changed_files_and_has_more_steps(self):
        a = self.make_split(
            [call_reply("run_subagent", prompt="make x", write=True), text_reply("final")],
            [call_reply("write_file", path="x.txt", content="hi")] + [call_reply("read_file", path="a.txt")] * 3
            + [text_reply("done")])
        a.run("task")
        res = a.history[2]["results"][0][1]
        self.assertEqual(res["changed_files"], ["x.txt"])
        self.assertEqual(a.subruns[1].agent.max_steps, agent_mod_limits()[1])

    def test_failed_write_subagent_lists_changed_files(self):
        a = self.make_split(
            [call_reply("run_subagent", prompt="make x", write=True), text_reply("final")],
            [call_reply("write_file", path="x.txt", content="hi"), RuntimeError("boom")])
        a.run("task")
        res = a.history[2]["results"][0][1]
        self.assertFalse(res["ok"])
        self.assertIn("x.txt", res["error"])

    def test_write_subagent_asks_confirmation(self):
        import builtins
        a = self.make_split(
            [call_reply("run_subagent", prompt="make x", write=True), text_reply("final")],
            [call_reply("write_file", path="x.txt", content="hi"), text_reply("denied")], auto_yes=False)
        real, builtins.input = builtins.input, lambda *_: "n"
        try:
            a.run("task")
        finally:
            builtins.input = real
        self.assertFalse((self.ws / "x.txt").exists())

    def test_background_write_without_yes_is_refused(self):
        a = self.make_split([call_reply("run_subagent", prompt="x", write=True, background=True), text_reply("f")],
                            [], auto_yes=False)
        a.run("task")
        self.assertIn("cannot ask", a.history[2]["results"][0][1]["error"])

    def test_background_subagent_and_get_result(self):
        a = self.make_split(
            [call_reply("run_subagent", prompt="one", background=True),
             call_reply("get_subagent_result", id=1),
             text_reply("final")],
            [call_reply("read_file", path="a.txt"), text_reply("bg answer")])
        self.assertEqual(a.run("task"), "final")
        started = a.history[2]["results"][0][1]
        self.assertEqual((started["ok"], started["id"]), (True, 1))
        self.assertEqual(a.history[4]["results"][0][1]["result"], "bg answer")
        self.assert_valid_history(a.history)

    def test_get_result_unknown_id(self):
        a = self.make_split([call_reply("get_subagent_result", id=7), text_reply("f")], [])
        a.run("task")
        self.assertIn("unknown subagent id", a.history[2]["results"][0][1]["error"])

    def test_show_subagent_replays_log(self):
        import contextlib
        import io
        a = self.make_split(
            [call_reply("run_subagent", prompt="look"), text_reply("final")],
            [call_reply("read_file", path="a.txt"), text_reply("seen")])
        a.run("task")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.handle(a, "/agents")
            commands.handle(a, "/agent 1")
        text = out.getvalue()
        self.assertIn("#1 [done]", text)
        self.assertIn("read_file", text)
        self.assertIn("seen", text)

    def test_parent_ctrl_c_cancels_background_subagent(self):
        gate = threading.Event()

        class Blocking(SplitLLM):
            def generate(self, history, system, schemas):
                if "subagent" in system:
                    gate.wait(5)  # субагент «думает», пока родитель получает Ctrl+C
                    return call_reply("read_file", path="a.txt")
                return super().generate(history, system, schemas)

        import agent as agent_mod
        self.llm = Blocking([call_reply("run_subagent", prompt="x", background=True), KeyboardInterrupt()], [])
        a = agent_mod.Agent(self.llm, self.ws, auto_yes=True, log_dir=self.dir / "logs")
        a.run("task")
        run = a.subruns[1]
        gate.set()
        run.thread.join(5)
        self.assertEqual(run.status, "cancelled")


if __name__ == "__main__":
    unittest.main()

"""Тесты /clear, /compact, CLAUDE.md, навыков и хуков.

Агент проверяется на той же фейковой модели, что и в test_agent.py; хуки — настоящие команды оболочки.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import commands  # noqa: E402
import main  # noqa: E402
import ui  # noqa: E402
import hooks as hooks_mod  # noqa: E402
import project  # noqa: E402
import tools  # noqa: E402
from llm import Reply  # noqa: E402
from tests.test_agent import AgentTestCase, FakeLLM, call_reply, text_reply  # noqa: E402


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestProject(AgentTestCase):
    def test_instructions_are_loaded_and_put_into_system_prompt(self):
        write(self.ws, "CLAUDE.md", "Use unittest, not pytest.")
        proj = project.load(self.ws)
        self.assertEqual(proj.instructions, "Use unittest, not pytest.")
        a = self.make([text_reply("ok")], project=proj)
        self.assertIn("Use unittest, not pytest.", a.system)
        self.assertTrue(a.system.startswith(agent_system_prompt()))

    def test_no_project_files_leaves_prompt_untouched(self):
        a = self.make([])
        self.assertEqual(a.system, agent_system_prompt())
        self.assertNotIn("use_skill", [s["name"] for s in a.schemas])

    def test_skill_parsing_and_listing(self):
        write(self.ws, ".claude/skills/review/SKILL.md",
              "---\nname: review\ndescription: Review the code\n---\nLook at $ARGUMENTS carefully.")
        write(self.ws, ".claude/skills/plain/SKILL.md", "# Plain skill\nDo things.")
        proj = project.load(self.ws)
        self.assertEqual(sorted(proj.skills), ["plain", "review"])
        self.assertEqual(proj.skills["plain"].description, "Plain skill")      # из первой строки тела
        self.assertIn("Look at stats.py carefully.", proj.skills["review"].render("stats.py"))
        self.assertIn("Arguments: x", proj.skills["plain"].render("x"))        # нет $ARGUMENTS: аргументы в конец
        a = self.make([], project=proj)
        self.assertIn("- review: Review the code", a.system)
        self.assertIn("use_skill", [s["name"] for s in a.schemas])

    def test_bad_skills_are_skipped_with_warning(self):
        write(self.ws, ".claude/skills/clear/SKILL.md", "---\ndescription: x\n---\nbody")      # имя встроенной команды
        write(self.ws, ".claude/skills/Bad Name/SKILL.md", "body")
        write(self.ws, ".claude/skills/empty/SKILL.md", "---\nname: empty\n---\n")
        proj = project.load(self.ws)
        self.assertEqual(proj.skills, {})
        self.assertEqual(len(proj.warnings), 3)

    def test_files_outside_workspace_via_symlink_are_ignored(self):
        outside = write(self.dir, "secret.md", "SECRET INSTRUCTIONS")
        (self.ws / "CLAUDE.md").symlink_to(outside)
        proj = project.load(self.ws)
        self.assertEqual(proj.instructions, "")
        self.assertTrue(any("за пределы" in w for w in proj.warnings))

    def test_use_skill_tool(self):
        write(self.ws, ".claude/skills/s/SKILL.md", "---\ndescription: d\n---\nDo the thing.")
        r = tools.execute(self.ws, "use_skill", {"name": "s"})
        self.assertTrue(r["ok"])
        self.assertIn("Do the thing.", r["instructions"])
        self.assertIn(".claude/skills/s", r["instructions"])
        bad = tools.execute(self.ws, "use_skill", {"name": "nope"})
        self.assertFalse(bad["ok"])
        self.assertIn("unknown skill", bad["error"])

    def test_settings_parsing(self):
        write(self.ws, ".claude/settings.json", json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "run_bash", "hooks": [{"type": "command", "command": "true", "timeout": 5}]}],
            "Nope": [], "Stop": [{"hooks": [{"command": ""}]}]}}))
        proj = project.load(self.ws)
        self.assertEqual([(h.event, h.matcher, h.timeout) for h in proj.hooks], [("PreToolUse", "run_bash", 5)])
        self.assertEqual(len(proj.warnings), 2)
        write(self.ws, ".claude/settings.json", "{broken")
        self.assertTrue(project.load(self.ws).warnings)


def agent_system_prompt():
    import agent
    return agent.SYSTEM_PROMPT


class TestHookRunner(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_hook(self, command, timeout=10, payload=None):
        return hooks_mod.run_command(command, payload or {}, self.dir, timeout)

    def test_stdin_json_cwd_and_exit_codes(self):
        r = self.run_hook("cat; pwd >&2; exit 2", payload={"tool_name": "x"})
        self.assertTrue(r.blocked)
        self.assertEqual(json.loads(r.stdout), {"tool_name": "x"})
        self.assertEqual(Path(r.stderr.strip()).resolve(), self.dir.resolve())
        self.assertTrue(self.run_hook("true").ok)
        r = self.run_hook("exit 1")
        self.assertFalse(r.ok or r.blocked)

    def test_hook_that_ignores_stdin_does_not_break(self):
        self.assertTrue(self.run_hook("echo hi", payload={"big": "x" * 1_000_000}).ok)

    def test_timeout_kills_the_whole_process_group(self):
        marker = self.dir / "alive"
        start = time.time()
        r = self.run_hook(f"(sleep 3; touch {marker}) & sleep 30", timeout=0.5)
        self.assertTrue(r.timed_out)
        self.assertIsNone(r.exit_code)
        self.assertLess(time.time() - start, 5)
        time.sleep(3.5)
        self.assertFalse(marker.exists())                                      # потомок убит вместе с shell

    def test_matcher(self):
        hs = hooks_mod.Hooks([hooks_mod.Hook("PreToolUse", "true", "run_bash|write_file"),
                              hooks_mod.Hook("PreToolUse", "true", ""),
                              hooks_mod.Hook("PreToolUse", "true", "edit"),
                              hooks_mod.Hook("Stop", "true")], self.dir)
        self.assertEqual(len(hs.matching("PreToolUse", "write_file")), 2)
        self.assertEqual(len(hs.matching("PreToolUse", "read_file")), 1)       # fullmatch: «edit» не совпадает с edit_file
        self.assertEqual(len(hs.matching("Stop")), 1)

    def test_blocking_hook_stops_the_rest(self):
        hs = hooks_mod.Hooks([hooks_mod.Hook("Stop", "exit 2"), hooks_mod.Hook("Stop", "true")], self.dir)
        self.assertEqual(len(hs.run("Stop")), 1)


class TestHooksInLoop(AgentTestCase):
    def make_hooked(self, replies, *hook_list, **kw):
        return self.make(replies, hooks=hooks_mod.Hooks(list(hook_list), self.ws), **kw)

    def results(self, a):
        return [r for m in a.history if "results" in m for _, r in m["results"]]

    def test_pre_tool_hook_blocks_call_and_model_sees_reason(self):
        hook = hooks_mod.Hook("PreToolUse", "echo 'no writing here' >&2; exit 2", "write_file")
        a = self.make_hooked([call_reply("write_file", path="x.txt", content="y"), text_reply("ok")], hook)
        a.run("go")
        self.assertFalse((self.ws / "x.txt").exists())
        self.assertIn("no writing here", self.results(a)[0]["error"])
        self.assertIn("PreToolUse", self.results(a)[0]["error"])
        self.assert_valid_history(a.history)

    def test_pre_tool_hook_blocks_before_confirmation_and_other_tools_unaffected(self):
        hook = hooks_mod.Hook("PreToolUse", "exit 2", "write_file")
        a = self.make_hooked([call_reply("write_file", path="x", content="y"), call_reply(path="a.txt"),
                              text_reply("ok")], hook)
        a.auto_yes = False
        with mock.patch("builtins.input", side_effect=AssertionError("не должен спрашивать")):
            a.run("go")
        self.assertTrue(self.results(a)[1]["ok"])                              # read_file не затронут

    def test_hook_receives_tool_input(self):
        out = self.dir / "payload.json"
        hook = hooks_mod.Hook("PreToolUse", f"cat > {out}", "read_file")
        self.make_hooked([call_reply(path="a.txt"), text_reply("ok")], hook).run("go")
        data = json.loads(out.read_text())
        self.assertEqual((data["hook_event_name"], data["tool_name"], data["tool_input"]),
                         ("PreToolUse", "read_file", {"path": "a.txt"}))

    def test_post_tool_hook_feedback_reaches_model(self):
        hook = hooks_mod.Hook("PostToolUse", "echo 'run the tests!' >&2; exit 2", "write_file")
        a = self.make_hooked([call_reply("write_file", path="x.txt", content="y"), text_reply("ok")], hook)
        a.run("go")
        self.assertTrue((self.ws / "x.txt").exists())                          # инструмент выполнен
        r = self.results(a)[0]
        self.assertTrue(r["ok"])
        self.assertEqual(r["hook_feedback"], "run the tests!")

    def test_non_blocking_failure_is_only_a_warning(self):
        hook = hooks_mod.Hook("PreToolUse", "echo oops >&2; exit 1")
        a = self.make_hooked([call_reply(path="a.txt"), text_reply("ok")], hook)
        self.assertEqual(a.run("go"), "ok")
        self.assertTrue(self.results(a)[0]["ok"])

    def test_user_prompt_hook_blocks_or_adds_context(self):
        a = self.make_hooked([text_reply("ok")], hooks_mod.Hook("UserPromptSubmit", "echo 'forbidden' >&2; exit 2"))
        msg = a.run("go")
        self.assertIn("forbidden", msg)
        self.assertEqual(a.history, [])                                         # модель задачу не получила
        a = self.make_hooked([text_reply("ok")], hooks_mod.Hook("UserPromptSubmit", "echo 'branch: main'"))
        a.run("go")
        self.assertIn("branch: main", a.history[0]["text"])
        self.assertTrue(a.history[0]["text"].startswith("go"))

    def test_stop_hook_returns_model_to_work_once(self):
        marker = self.dir / "ran"
        hook = hooks_mod.Hook("Stop", f"touch {marker}; echo 'tests are failing' >&2; exit 2")
        a = self.make_hooked([text_reply("done"), text_reply("fixed it")], hook)
        self.assertEqual(a.run("go"), "fixed it")
        self.assertIn("tests are failing", a.history[2]["text"])
        self.assert_valid_history(a.history)
        self.assertEqual([m["role"] for m in a.history], ["user", "model", "user", "model"])

    def test_stop_hook_cannot_loop_forever(self):
        a = self.make_hooked([text_reply("a"), text_reply("b"), text_reply("c")], hooks_mod.Hook("Stop", "exit 2"))
        self.assertEqual(a.run("go"), "b")                                      # второй раз хук уже не спрашивают

    def test_hooks_are_logged(self):
        a = self.make_hooked([call_reply(path="a.txt"), text_reply("ok")], hooks_mod.Hook("PostToolUse", "true"))
        a.run("go")
        events = [json.loads(line) for line in a.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["hook_event"] for e in events if e["event"] == "hook"], ["PostToolUse"])


class CompactLLM(FakeLLM):
    def __init__(self, replies, summary="SUMMARY"):
        super().__init__(replies)
        self.summary, self.summarized = summary, []

    def summarize(self, history, instructions=""):
        self.summarized.append((list(history), instructions))
        if isinstance(self.summary, BaseException):
            raise self.summary
        return self.summary


class TestClearCompact(AgentTestCase):
    def make_c(self, replies, **kw):
        import agent
        return agent.Agent(CompactLLM(replies, **kw), self.ws, auto_yes=True, log_dir=self.dir / "logs")

    def test_clear_empties_history_and_resume_respects_it(self):
        a = self.make([text_reply("hi")])
        a.run("one")
        a.clear()
        self.assertEqual(a.history, [])
        a.llm.replies = [text_reply("second")]
        a.run("two")
        self.assertEqual([m["text"] for m in a.history], ["two", "second"])
        b = self.make([])
        b.resume(a.log_path)
        self.assertEqual(b.history, a.history)                                  # старый разговор не воскрес

    def test_resume_after_clear_only_gives_empty_history(self):
        a = self.make([text_reply("hi")])
        a.run("one")
        a.clear()
        b = self.make([])
        self.assertEqual(b.resume(a.log_path), 0)
        self.assertEqual(b.history, [])

    def test_compact_replaces_history_with_summary(self):
        a = self.make_c([call_reply(path="a.txt"), text_reply("done")])
        a.run("task")
        self.assertTrue(a.compact("focus on files"))
        self.assertEqual(a.llm.summarized[0][1], "focus on files")
        self.assertEqual([m["role"] for m in a.history], ["user", "model"])
        self.assertIn("SUMMARY", a.history[0]["text"])
        self.assert_valid_history(a.history)
        a.llm.replies = [text_reply("continuing")]
        self.assertEqual(a.run("next"), "continuing")                           # агент работает дальше
        self.assert_valid_history(a.history)
        self.assertEqual([m["role"] for m in a.history], ["user", "model", "user", "model"])

    def test_compact_survives_resume(self):
        a = self.make_c([text_reply("done")])
        a.run("task")
        a.compact()
        a.llm.replies = [text_reply("more")]
        a.run("next")
        b = self.make_c([])
        b.resume(a.log_path)
        self.assertEqual(b.history, a.history)
        self.assertEqual(len(b.history), 4)

    def test_failed_or_interrupted_compact_keeps_history(self):
        for error in (RuntimeError("boom"), KeyboardInterrupt()):
            a = self.make_c([text_reply("done")], summary=error)
            a.run("task")
            before = list(a.history)
            self.assertFalse(a.compact())
            self.assertEqual(a.history, before)

    def test_compact_of_empty_history_does_nothing(self):
        a = self.make_c([])
        self.assertFalse(a.compact())
        self.assertEqual(a.llm.summarized, [])


class TestCommands(AgentTestCase):
    def test_clear_compact_help_and_unknown(self):
        a = self.make([text_reply("hi")])
        a.run("x")
        self.assertTrue(commands.handle(a, "/help"))
        self.assertTrue(commands.handle(a, "/nonsense"))
        self.assertEqual(len(a.history), 2)                                      # неизвестная команда ничего не меняет
        self.assertTrue(commands.handle(a, "/clear"))
        self.assertEqual(a.history, [])

    def test_compact_command_passes_arguments(self):
        a = self.make([])
        with mock.patch.object(a, "compact") as compact:
            commands.handle(a, "/compact  keep the file list ")
        compact.assert_called_once_with("keep the file list")

    def test_paths_are_not_commands(self):
        a = self.make([])
        self.assertFalse(commands.handle(a, "/tmp/x.py is broken"))
        self.assertFalse(commands.handle(a, "/Users/me/x"))

    def test_skill_command_runs_agent_with_rendered_skill(self):
        write(self.ws, ".claude/skills/greet/SKILL.md", "---\ndescription: say hi\n---\nGreet $ARGUMENTS.")
        a = self.make([text_reply("hello!")], project=project.load(self.ws))
        self.assertTrue(commands.handle(a, "/greet Bob"))
        self.assertIn("Greet Bob.", a.history[0]["text"])
        self.assertEqual(a.history[-1]["text"], "hello!")


class TestConfirmHooks(AgentTestCase):
    """Запрос подтверждения хуков при старте (main.confirm_hooks)."""

    def hooks(self):
        return hooks_mod.Hooks([hooks_mod.Hook("PreToolUse", "echo x", "run_bash")], self.ws)

    def ask(self, answer=None, error=None, auto_yes=False):
        h = self.hooks()
        with mock.patch("builtins.input", side_effect=[answer] if error is None else error) as inp:
            result = main.confirm_hooks(h, auto_yes)
        return h, result, inp

    def test_yes_keeps_hooks(self):
        for answer in ("y", "YES", "д", " да "):
            h, result, _ = self.ask(answer)
            self.assertIs(result, h)

    def test_anything_else_disables_hooks(self):
        for answer in ("", "n", "no", "yy"):
            _, result, _ = self.ask(answer)
            self.assertFalse(result)
            self.assertEqual(result.workspace, self.ws)

    def test_eof_and_ctrl_c_disable_hooks(self):
        for error in (EOFError(), KeyboardInterrupt()):
            self.assertFalse(self.ask(error=error)[1])

    def test_auto_yes_does_not_ask(self):
        h = self.hooks()
        with mock.patch("builtins.input", side_effect=AssertionError("не должен спрашивать")):
            self.assertIs(main.confirm_hooks(h, True), h)

    def test_no_hooks_does_not_ask(self):
        empty = hooks_mod.Hooks([], self.ws)
        with mock.patch("builtins.input", side_effect=AssertionError("не должен спрашивать")):
            self.assertIs(main.confirm_hooks(empty, False), empty)

    def test_disabled_hooks_do_not_run_in_agent(self):
        marker = self.dir / "ran"
        h = hooks_mod.Hooks([hooks_mod.Hook("Stop", f"touch {marker}")], self.ws)
        with mock.patch("builtins.input", return_value="n"):
            h = main.confirm_hooks(h, False)
        self.make([text_reply("ok")], hooks=h).run("go")
        self.assertFalse(marker.exists())


class TestUiAndRender(unittest.TestCase):
    def capture(self, fn, *args, **kw):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args, **kw)
        return buf.getvalue()

    def test_hook_output_is_short_and_multiline_is_capped(self):
        out = self.capture(ui.hook, "PreToolUse", "python3 " + "x" * 200, "блокировка: " + "\n".join(f"l{i}" for i in range(10)))
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)                                          # заголовок + не больше 3 строк
        self.assertIn("PreToolUse", lines[0])
        self.assertLess(max(len(line) for line in lines), 140)
        self.assertEqual(self.capture(ui.hook, "Stop", "c", "").count("\n"), 1)  # пустой текст не падает

    def test_banner_shows_project_row_only_when_given(self):
        self.assertIn("проект: CLAUDE.md, навыков: 2", self.capture(ui.banner, "m", "/w", "log", "CLAUDE.md, навыков: 2"))
        self.assertNotIn("проект:", self.capture(ui.banner, "m", "/w", "log"))

    def test_skill_render_says_it_is_already_loaded(self):
        skill = project.Skill("s", "d", "Body $ARGUMENTS", ".claude/skills/s")
        text = skill.render("x")
        self.assertIn("already loaded", text)
        self.assertIn("do not call use_skill", text)
        self.assertIn("Body x", text)
        self.assertIn(".claude/skills/s/", text)


class TestCtrlCInHooks(AgentTestCase):
    """Ctrl+C во время хука: REPL не падает, история остаётся валидной, агент пригоден дальше."""

    def interrupted(self, event, replies):
        hook = hooks_mod.Hook(event, "true")
        a = self.make(replies, hooks=hooks_mod.Hooks([hook], self.ws))
        with mock.patch.object(hooks_mod, "run_command", side_effect=KeyboardInterrupt):
            msg = a.run("go")
        self.assertIn("Прервано", msg)
        self.assert_valid_history(a.history)
        a.hooks = hooks_mod.Hooks([], self.ws)
        a.llm.replies = [text_reply("fine")]
        self.assertEqual(a.run("again"), "fine")                                # агент пригоден дальше
        self.assert_valid_history(a.history)
        return a

    def test_user_prompt_submit(self):
        a = self.interrupted("UserPromptSubmit", [])
        self.assertEqual([m["role"] for m in a.history], ["user", "model"])      # прерванная задача в историю не попала

    def test_pre_tool_use(self):
        self.interrupted("PreToolUse", [call_reply(path="a.txt")])

    def test_post_tool_use(self):
        self.interrupted("PostToolUse", [call_reply(path="a.txt")])

    def test_stop(self):
        self.interrupted("Stop", [text_reply("done")])

    def test_runner_kills_hook_on_keyboard_interrupt(self):
        marker = self.dir / "alive"
        real = hooks_mod.subprocess.Popen.communicate

        def communicate(self_, *a, **kw):
            raise KeyboardInterrupt

        with mock.patch.object(hooks_mod.subprocess.Popen, "communicate", communicate):
            with self.assertRaises(KeyboardInterrupt):
                hooks_mod.run_command(f"sleep 3; touch {marker}", {}, self.ws, 10)
        time.sleep(3.5)
        self.assertFalse(marker.exists())                                        # процесс хука убит


if __name__ == "__main__":
    unittest.main()

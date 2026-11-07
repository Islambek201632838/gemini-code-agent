"""Тесты run_bash: убийство группы процессов, stdin, лимит вывода во время чтения, стриминг, Ctrl+C."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tools  # noqa: E402


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestRunBash(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def run_bash(self, command, **kw):
        return tools.execute(self.ws, "run_bash", {"command": command, **kw})

    def _child_pid(self) -> int:
        return int((self.ws / "child.pid").read_text())

    def _assert_child_dead(self):
        pid = self._child_pid()
        for _ in range(20):  # SIGKILL доставляется не мгновенно
            if not _alive(pid):
                return
            time.sleep(0.1)
        os.kill(pid, 9)
        self.fail("дочерний процесс пережил убийство группы")

    def test_timeout_kills_whole_process_group(self):
        t0 = time.monotonic()
        r = self.run_bash("sleep 60 & echo $! > child.pid; wait", timeout=1)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["error"])
        self._assert_child_dead()

    def test_stdin_is_closed(self):
        t0 = time.monotonic()
        r = self.run_bash("cat; echo after", timeout=10)  # cat не должен ждать ввод с терминала
        self.assertTrue(r["ok"])
        self.assertIn("after", r["output"])
        self.assertLess(time.monotonic() - t0, 5)

    def test_output_is_capped_while_reading(self):
        r = self.run_bash("head -c 5000000 /dev/zero | tr '\\0' x", timeout=30)
        self.assertTrue(r["ok"])
        self.assertIn("truncated: 4990000 more characters", r["output"])
        self.assertLess(len(r["output"]), tools.MAX_OUTPUT_CHARS + 100)

    def test_stderr_is_separated_and_exit_code_kept(self):
        r = self.run_bash("echo out; echo err 1>&2; exit 3")
        self.assertFalse(r["ok"])
        self.assertEqual(r["exit_code"], 3)
        self.assertIn("out", r["output"])
        self.assertIn("[stderr]\nerr", r["output"])

    def test_output_is_streamed_before_command_ends(self):
        events = []
        t0 = time.monotonic()
        tools.execute(self.ws, "run_bash", {"command": "echo first; sleep 1; echo second"},
                      on_output=lambda s, t: events.append((time.monotonic() - t0, s, t)))
        first = next(e for e in events if "first" in e[2])
        second = next(e for e in events if "second" in e[2])
        self.assertLess(first[0], 0.8)           # пришло раньше, чем команда завершилась
        self.assertGreater(second[0], 0.9)

    def test_model_cannot_inject_its_own_callback(self):
        r = tools.execute(self.ws, "run_bash", {"command": "echo hi", "on_output": "boom"})
        self.assertTrue(r["ok"])

    def test_ctrl_c_kills_group_and_propagates(self):
        def interrupt(stream, text):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            tools.execute(self.ws, "run_bash",
                          {"command": "sleep 60 & echo $! > child.pid; echo go; wait", "timeout": 30},
                          on_output=interrupt)
        self._assert_child_dead()


if __name__ == "__main__":
    unittest.main()

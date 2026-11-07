"""Цикл агента, написанный вручную.

    задача пользователя -> [ модель -> вызовы инструментов? -> выполнить -> вернуть результаты ] -> итоговый текст

Цикл заканчивается, когда модель отвечает обычным текстом (без вызовов инструментов)
или когда достигнут max_steps (защита от бесконечных циклов и неконтролируемых расходов).
"""
import json
import time
from datetime import datetime
from pathlib import Path

import tools
import ui
from llm import LLM

SYSTEM_PROMPT = """You are a coding assistant working inside a workspace directory.
- Explore before editing: use glob/grep to find files and read_file to understand them.
- Make small, targeted edits with edit_file; use write_file only for new files.
- Verify your work by running the tests with run_bash.
- When the task is done, stop and reply with a brief summary of what you changed."""


class Agent:
    def __init__(self, llm: LLM, workspace: Path, auto_yes: bool = False,
                 max_steps: int = 25, log_dir: Path = Path("logs")):
        self.llm = llm
        self.workspace = workspace.resolve()
        self.auto_yes = auto_yes
        self.max_steps = max_steps
        self.history: list = []  # непрозрачные сообщения провайдера; сохраняются между задачами в одной сессии REPL
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"run-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    def _log(self, event: str, **data):
        with self.log_path.open("a", encoding="utf-8") as f:
            # ensure_ascii=False: кириллица пишется как есть, а не как \uXXXX
            line = json.dumps({"ts": time.time(), "event": event, **data}, default=str, ensure_ascii=False)
            f.write(line + "\n")

    def _confirm(self, name: str, args: dict) -> bool:
        if self.auto_yes or name not in tools.NEEDS_CONFIRMATION:
            return True
        # ui показывает действие целиком (команду, diff, содержимое): пользователь одобряет именно это.
        return input(ui.confirm_request(name, args)).strip().lower() in ("y", "yes", "д", "да")

    def run(self, task: str) -> str:
        self.history.append(self.llm.user_message(task))
        self._log("task", task=task)

        for step in range(1, self.max_steps + 1):
            try:
                reply = self.llm.generate(self.history, SYSTEM_PROMPT, tools.SCHEMAS)
            except Exception as e:  # API не ответил даже после повторов: сообщаем и не роняем REPL
                msg = f"Вызов LLM не удался: {type(e).__name__}: {e}"
                self._log("llm_error", step=step, error=msg)
                ui.error(msg)
                return msg

            self.history.append(reply.message)  # без изменений: сохраняет thought signatures целыми
            if reply.text:
                ui.model_text(reply.text)  # финальный ответ тоже печатается здесь, main его не дублирует
            ui.usage(step, self.max_steps, reply.usage)
            self._log("model", step=step, text=reply.text, usage=reply.usage,
                      tool_calls=[{"name": c.name, "args": c.args} for c in reply.tool_calls])

            if not reply.tool_calls:  # обычный текст = модель считает задачу выполненной
                return reply.text

            results = []
            for call in reply.tool_calls:
                ui.tool_call(call.name, call.args)
                if self._confirm(call.name, call.args):
                    result = tools.execute(self.workspace, call.name, call.args)
                else:
                    result = {"ok": False, "error": "user denied this action"}
                ui.tool_result(result)
                self._log("tool", step=step, name=call.name, args=call.args, result=result)
                results.append((call, result))
            self.history.append(self.llm.tool_results_message(results))

        msg = f"Остановлено: достигнут лимит шагов ({self.max_steps}), финального ответа нет."
        self._log("max_steps", step=self.max_steps)
        ui.error(msg)
        return msg

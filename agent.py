"""Цикл агента, написанный вручную.

    задача пользователя -> [ модель -> вызовы инструментов? -> выполнить -> вернуть результаты ] -> итоговый текст

Цикл заканчивается, когда модель отвечает обычным текстом (без вызовов инструментов),
когда достигнут max_steps (защита от бесконечных циклов и неконтролируемых расходов),
когда модель зациклилась на одном и том же вызове или когда пользователь нажал Ctrl+C.
В двух последних случаях история остаётся корректной: на каждый вызов есть результат.

Вокруг цикла: хуки (hooks.py), контекст проекта (project.py: CLAUDE.md и навыки в системном промпте)
и управление историей: clear() сбрасывает её, compact() заменяет пересказом.
"""
import json
import time
from datetime import datetime
from pathlib import Path

import tools
import ui
from hooks import Hooks
from llm import LLM
from project import Project

SYSTEM_PROMPT = """You are a coding assistant working inside a workspace directory.
- Explore before editing: use glob/grep to find files and read_file to understand them.
- Make small, targeted edits with edit_file; use write_file only for new files.
- Verify your work by running the tests with run_bash.
- Before every tool call (or group of calls), write one short sentence saying what you are about to do and why,
  so the user can follow along. Never call tools silently.
  Write all text for the user in the language of the user's request.
- If a call fails or gets blocked, change your approach instead of repeating the same call.
- When the task is done, stop and reply with a brief summary of what you changed."""

COMPACT_ACK = "Understood. I have the summary of the earlier conversation and will continue from it."

REPEAT_BLOCK = 3  # столько одинаковых вызовов подряд: третий не выполняется, модель получает подсказку сменить подход
REPEAT_STOP = 5   # а на пятом цикл останавливается


def find_latest_log(log_dir: Path = Path("logs")) -> Path | None:
    """Самый свежий лог, из которого можно восстановить историю (в нём есть события message)."""
    for path in sorted(log_dir.glob("run-*.jsonl"), reverse=True):
        if '"event": "message"' in path.read_text(encoding="utf-8"):
            return path
    return None


class Agent:
    def __init__(self, llm: LLM, workspace: Path, auto_yes: bool = False,
                 max_steps: int = 25, log_dir: Path = Path("logs"),
                 project: Project | None = None, hooks: Hooks | None = None):
        self.llm = llm
        self.workspace = workspace.resolve()
        self.project = project or Project()
        self.hooks = hooks or Hooks([], self.workspace)
        self.system = self.project.system_prompt(SYSTEM_PROMPT)  # + навыки и CLAUDE.md
        # use_skill имеет смысл, только если есть навыки: иначе не показываем его модели
        self.schemas = [s for s in tools.SCHEMAS if self.project.skills or s["name"] != "use_skill"]
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

    def _add(self, message) -> None:
        """Положить сообщение в историю и в лог в исходном виде (с thought signatures) для --resume."""
        self.history.append(message)
        self._log("message", data=self.llm.dump_message(message))

    def resume(self, path: Path) -> int:
        """Восстановить историю из JSONL-лога и продолжить писать в тот же файл. Возвращает число сообщений."""
        messages, seen = [], False
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue  # оборванная последняя строка после аварийного завершения
            if event.get("event") == "message":
                messages.append(self.llm.load_message(event["data"]))
                seen = True
            elif event.get("event") == "reset":  # /clear или /compact: всё до этой точки в историю не входит
                messages = []
        if not seen:
            raise ValueError(f"в {path} нет сообщений модели (лог старого формата?): восстановить нечего")
        self.history = messages
        self.log_path = path
        # Если сессия оборвалась на вызове инструмента (kill -9, падение), API отклонит историю без ответов.
        dangling = self.llm.unanswered_calls(self.history)
        if dangling:
            self._close_turn(dangling, [], "session was interrupted before this call ran",
                             "[session was interrupted]")
        return len(messages)

    # --- управление историей (/clear, /compact) ---

    def clear(self) -> None:
        """Начать разговор с чистого листа. Файл лога тот же; событие reset говорит --resume забыть прошлое."""
        self.history = []
        self._log("reset", reason="clear")

    def compact(self, instructions: str = "") -> bool:
        """Заменить историю пересказом, сделанным моделью. Возвращает True, если сжатие удалось.

        До успешного ответа модели история не трогается, поэтому ошибка API или Ctrl+C ничего не ломают.
        Новая история: пересказ как реплика пользователя + подтверждение модели (роли чередуются).
        """
        if not self.history:
            ui.info("История пуста: сжимать нечего.")
            return False
        before = len(self.history)
        try:
            summary = self.llm.summarize(self.history, instructions)
        except KeyboardInterrupt:
            ui.error("Сжатие прервано, история не изменена.")
            return False
        except Exception as e:
            msg = f"Сжатие не удалось, история не изменена: {type(e).__name__}: {e}"
            self._log("llm_error", step=0, error=msg)
            ui.error(msg)
            return False
        self.history = []
        self._log("reset", reason="compact")
        self._add(self.llm.user_message("Summary of the earlier conversation (the full history was compacted):\n\n"
                                        + summary))
        self._add(self.llm.model_message(COMPACT_ACK))
        ui.info(f"История сжата: {before} сообщений -> {len(self.history)}, пересказ {len(summary)} символов.")
        return True

    # --- хуки ---

    def _run_hooks(self, event: str, payload: dict, tool_name: str | None = None) -> tuple[str | None, str]:
        """Выполнить хуки события. Возвращает (причина блокировки или None, stdout успешных хуков)."""
        block, out = None, []
        for hook, res in self.hooks.run(event, payload, tool_name):
            self._log("hook", hook_event=event, command=hook.command, exit_code=res.exit_code,
                      timed_out=res.timed_out, stdout=res.stdout, stderr=res.stderr)
            if res.blocked:
                block = res.stderr.strip() or "(hook gave no reason)"
                ui.hook(event, hook.command, f"блокировка: {block}", failed=True)
            elif res.ok:
                if res.stdout.strip():
                    out.append(res.stdout.strip())
                    ui.hook(event, hook.command, res.stdout.strip())
            else:
                why = "таймаут" if res.timed_out else f"код выхода {res.exit_code}"
                ui.hook(event, hook.command, f"{why} (работа продолжается)" + (f": {res.stderr.strip()}" if res.stderr.strip() else ""),
                        failed=True)
        return block, "\n".join(out)

    def _close_turn(self, calls: list, results: list, reason: str, note: str) -> None:
        """Закрыть ход так, чтобы история осталась валидной: ответ на КАЖДЫЙ вызов + сообщение модели.

        Без этого следующий запрос к API отклонят (вызов без результата), а две реплики пользователя
        подряд нарушают чередование ролей.
        """
        answered = {id(c) for c, _ in results}
        results = results + [(c, {"ok": False, "error": reason}) for c in calls if id(c) not in answered]
        if results:
            self._add(self.llm.tool_results_message(results))
        self._add(self.llm.model_message(note))

    def _execute(self, call) -> tuple[dict, bool]:
        """Выполнить вызов. Для run_bash вывод стримится в консоль; второй элемент: был ли он показан."""
        if call.name != "run_bash":
            return tools.execute(self.workspace, call.name, call.args), False
        live = ui.LiveOutput()
        try:
            return tools.execute(self.workspace, call.name, call.args, on_output=live), live.shown
        finally:
            live.close()

    def _confirm(self, name: str, args: dict) -> bool:
        if self.auto_yes or name not in tools.NEEDS_CONFIRMATION:
            return True
        # ui показывает действие целиком (команду, diff, содержимое): пользователь одобряет именно это.
        return input(ui.confirm_request(name, args)).strip().lower() in ("y", "yes", "д", "да")

    def run(self, task: str) -> str:
        try:
            block, context = self._run_hooks("UserPromptSubmit", {"prompt": task})
        except KeyboardInterrupt:  # задача ещё не в истории: отменять нечего
            return self._cancelled_outside_turn()
        if block:  # задача не попадает ни в историю, ни к модели
            msg = f"Задача заблокирована хуком UserPromptSubmit: {block}"
            self._log("task_blocked", task=task, reason=block)
            ui.error(msg)
            return msg
        self._add(self.llm.user_message(task + (f"\n\n[Context from UserPromptSubmit hook]\n{context}" if context else "")))
        self._log("task", task=task)

        pending: list | None = None  # вызовы хода, для которых tool_results ещё не в истории (нужно при Ctrl+C)
        results: list = []
        last_sig, repeat = None, 0   # защита от зацикливания: сколько раз подряд один и тот же вызов
        stop_hook_used = False       # Stop-хук может вернуть модель к работе только один раз за задачу
        try:
            for step in range(1, self.max_steps + 1):
                try:
                    reply = self.llm.generate(self.history, self.system, self.schemas)
                except Exception as e:  # API не ответил даже после повторов: сообщаем и не роняем REPL
                    msg = f"Вызов LLM не удался: {type(e).__name__}: {e}"
                    self._log("llm_error", step=step, error=msg)
                    ui.error(msg)
                    return msg

                self._add(reply.message)  # без изменений: сохраняет thought signatures целыми
                if reply.text:
                    ui.model_text(reply.text)  # пояснение к вызову и финальный ответ; main ничего не дублирует
                ui.usage(step, self.max_steps, reply.usage)
                self._log("model", step=step, text=reply.text, usage=reply.usage,
                          tool_calls=[{"name": c.name, "args": c.args} for c in reply.tool_calls])

                if not reply.tool_calls:  # обычный текст = модель считает задачу выполненной
                    if not stop_hook_used:
                        try:
                            block, _ = self._run_hooks("Stop", {"last_assistant_message": reply.text})
                        except KeyboardInterrupt:  # ответ модели уже в истории: _close_turn дал бы две реплики модели подряд
                            return self._cancelled_outside_turn()
                        if block:  # хук не согласен с завершением: его сообщение модели, работа продолжается
                            stop_hook_used = True
                            self._add(self.llm.user_message(f"[Stop hook] {block}"))
                            continue
                    return reply.text

                pending, results = reply.tool_calls, []
                for call in pending:
                    ui.tool_call(call.name, call.args)
                    sig = (call.name, json.dumps(call.args, sort_keys=True, default=str))
                    repeat = repeat + 1 if sig == last_sig else 1
                    last_sig = sig
                    if repeat >= REPEAT_STOP:
                        msg = (f"Остановлено: модель {repeat} раз подряд вызвала {call.name} "
                               "с одними и теми же аргументами (зацикливание).")
                        self._log("loop_stop", step=step, name=call.name, args=call.args)
                        return self._stop(pending, results, "stopped: repeated identical call", msg)

                    shown = False
                    if repeat >= REPEAT_BLOCK:  # не выполняем, а подсказываем сменить подход
                        result = {"ok": False, "error": f"this exact call was already made {repeat - 1} times in a "
                                  "row with the same arguments and was NOT run again; change your approach "
                                  "or explain to the user what is blocking you"}
                    elif block := self._run_hooks("PreToolUse", {"tool_input": call.args}, call.name)[0]:
                        result = {"ok": False, "error": f"blocked by a PreToolUse hook: {block}"}
                    elif self._confirm(call.name, call.args):
                        result, shown = self._execute(call)
                        feedback = self._run_hooks("PostToolUse", {"tool_input": call.args, "tool_response": result},
                                                   call.name)[0]
                        if feedback:  # хук просит модель обратить внимание на результат
                            result = {**result, "hook_feedback": feedback}
                    else:
                        result = {"ok": False, "error": "user denied this action"}
                    ui.tool_result(result, streamed=shown)
                    self._log("tool", step=step, name=call.name, args=call.args, result=result)
                    results.append((call, result))
                self._add(self.llm.tool_results_message(results))
                pending = None

            msg = f"Остановлено: достигнут лимит шагов ({self.max_steps}), финального ответа нет."
            self._log("max_steps", step=self.max_steps)
            ui.error(msg)
            return msg
        except KeyboardInterrupt:
            # Ctrl+C: прерываем запрос к модели или инструмент, но историю оставляем валидной.
            self._log("cancelled")
            return self._stop(pending or [], results, "cancelled by user", "Прервано пользователем (Ctrl+C).")

    def _cancelled_outside_turn(self) -> str:
        """Ctrl+C там, где история уже валидна (хук до начала задачи или после финального ответа): ничего не закрываем."""
        self._log("cancelled")
        msg = "Прервано пользователем (Ctrl+C)."
        ui.error(msg)
        return msg

    def _stop(self, pending: list, results: list, reason: str, msg: str) -> str:
        self._close_turn(pending, results, reason, f"[{reason}]")  # для модели: по-английски
        ui.error(msg)
        return msg

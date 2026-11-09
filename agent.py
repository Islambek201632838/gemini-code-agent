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
import threading
import time
from dataclasses import dataclass
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

# Дописывается к системному промпту в режиме планирования (/plan) и у субагента (только чтение).
PLAN_SUFFIX = """PLAN MODE is on: you are read-only. Only read_file, glob and grep are available; nothing can be changed.
Investigate the task, then reply with a short numbered plan: which files you would change and how, and how to verify it.
Do not try to make changes. The user will leave plan mode and ask you to carry the plan out."""
SUBAGENT_SUFFIX = """You are a read-only research subagent. Only read_file, glob and grep are available.
Investigate the task you were given and finish with a concise, self-contained summary of your findings
(file paths and line numbers where useful). Your final message is the ONLY thing the caller will see."""

# Если субагенту разрешена запись (run_subagent(write=true)), дописывается вместо «read-only».
SUBAGENT_WRITE_SUFFIX = """You are a subagent that may also change files: read_file, glob, grep, write_file and edit_file are available
(no shell). Do exactly the task you were given, keep edits small, and finish with a concise summary of what you
changed (file paths). Your final message is the ONLY thing the caller will see."""

WRITE_TOOLS = {"write_file", "edit_file"}  # что добавляется субагенту при write=true

SUBAGENT_WRITE_MAX_STEPS = 25  # субагенту с записью нужно больше: отказы пользователя и перечитывание файлов съедают шаги
SUBAGENT_MAX_STEPS = 12  # субагент не должен тратить столько же шагов, сколько основной агент

REPEAT_BLOCK = 3  # столько одинаковых вызовов подряд: третий не выполняется, модель получает подсказку сменить подход
REPEAT_STOP = 5   # а на пятом цикл останавливается


def find_latest_log(log_dir: Path = Path("logs")) -> Path | None:
    """Самый свежий лог, из которого можно восстановить историю (в нём есть события message)."""
    for path in sorted(log_dir.glob("run-*.jsonl"), reverse=True):
        if '"event": "message"' in path.read_text(encoding="utf-8"):
            return path
    return None


@dataclass
class SubRun:
    """Запущенный субагент: нужен для фонового режима, get_subagent_result и просмотра /agent N."""
    id: int
    prompt: str
    agent: "Agent"
    write: bool = False
    thread: threading.Thread | None = None   # None: субагент шёл в переднем плане
    answer: str = ""
    reported: bool = False                   # сообщили ли пользователю о завершении фонового субагента

    @property
    def status(self) -> str:
        if self.thread and self.thread.is_alive():
            return "running"
        return {"done": "done", "cancelled": "cancelled"}.get(self.agent.outcome, "failed")


class Agent:
    def __init__(self, llm: LLM, workspace: Path, auto_yes: bool = False,
                 max_steps: int = 25, log_dir: Path = Path("logs"),
                 project: Project | None = None, hooks: Hooks | None = None,
                 compact_at: int | None = None, read_only: bool = False, log_name: str | None = None,
                 ui_out=None, can_write: bool = False):
        self.llm = llm
        self.ui = ui_out or ui  # у субагентов: ui.Nested (передний план) или ui.Quiet (фон)
        self.workspace = workspace.resolve()
        self.project = project or Project()
        self.hooks = hooks or Hooks([], self.workspace)
        self.read_only = read_only  # субагент: всегда только чтение, без собственных субагентов
        self.system = self.project.system_prompt(SYSTEM_PROMPT)  # + навыки и CLAUDE.md
        self.can_write = read_only and can_write  # субагент с правом на write_file/edit_file (без run_bash)
        if read_only:
            self.system += "\n\n" + (SUBAGENT_WRITE_SUFFIX if self.can_write else SUBAGENT_SUFFIX)
        # use_skill имеет смысл, только если есть навыки: иначе не показываем его модели
        self.schemas = [s for s in tools.SCHEMAS if self.project.skills or s["name"] != "use_skill"]
        self.auto_yes = auto_yes
        self.max_steps = max_steps
        self.compact_at = compact_at or None  # порог usage.prompt в токенах для автосжатия; None/0: выключено
        self.last_prompt: int | None = None   # размер промпта по последнему ответу модели (None: неизвестен)
        self.plan_mode = False                # /plan: доступны только инструменты чтения
        self.outcome = "failed"               # как закончился последний run(): done / cancelled / failed
        self._subagents = 0
        self.subruns: dict[int, SubRun] = {}
        self.changed: list[str] = []          # файлы, успешно изменённые write_file/edit_file этим агентом
        self._cancel = threading.Event()      # выставляется родителем: фоновый субагент остановится на ближайшем шаге
        self.history: list = []  # непрозрачные сообщения провайдера; сохраняются между задачами в одной сессии REPL
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / (log_name or f"run-{datetime.now():%Y%m%d-%H%M%S}.jsonl")

    # --- режим планирования ---

    def set_plan_mode(self, on: bool) -> None:
        self.plan_mode = on
        self._log("mode", plan=on)

    def _allowed(self, name: str) -> bool:
        """В режиме планирования и у субагента доступны только инструменты чтения."""
        if self.plan_mode:
            return name in tools.READ_ONLY_TOOLS
        if self.read_only:
            return name in tools.READ_ONLY_TOOLS or (self.can_write and name in WRITE_TOOLS)
        return True

    def _system(self) -> str:
        return self.system + "\n\n" + PLAN_SUFFIX if self.plan_mode else self.system

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
        self.last_prompt = None
        self._log("reset", reason="clear")

    def compact(self, instructions: str = "") -> bool:
        """Заменить историю пересказом, сделанным моделью. Возвращает True, если сжатие удалось.

        До успешного ответа модели история не трогается, поэтому ошибка API или Ctrl+C ничего не ломают.
        Новая история: пересказ как реплика пользователя + подтверждение модели (роли чередуются).
        """
        if not self.history:
            self.ui.info("История пуста: сжимать нечего.")
            return False
        before = len(self.history)
        try:
            summary = self.llm.summarize(self.history, instructions)
        except KeyboardInterrupt:
            self.ui.error("Сжатие прервано, история не изменена.")
            return False
        except Exception as e:
            msg = f"Сжатие не удалось, история не изменена: {type(e).__name__}: {e}"
            self._log("llm_error", step=0, error=msg)
            self.ui.error(msg)
            return False
        self.history = []
        self.last_prompt = None
        self._log("reset", reason="compact")
        self._add(self.llm.user_message("Summary of the earlier conversation (the full history was compacted):\n\n"
                                        + summary))
        self._add(self.llm.model_message(COMPACT_ACK))
        self.ui.info(f"История сжата: {before} сообщений -> {len(self.history)}, пересказ {len(summary)} символов.")
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
                self.ui.hook(event, hook.command, f"блокировка: {block}", failed=True)
            elif res.ok:
                if res.stdout.strip():
                    out.append(res.stdout.strip())
                    self.ui.hook(event, hook.command, res.stdout.strip())
            else:
                why = "таймаут" if res.timed_out else f"код выхода {res.exit_code}"
                self.ui.hook(event, hook.command, f"{why} (работа продолжается)" + (f": {res.stderr.strip()}" if res.stderr.strip() else ""),
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
        if call.name == "run_subagent":  # нужен доступ к LLM, поэтому не в tools.py
            return self._run_subagent(call.args), False
        if call.name == "get_subagent_result":
            return self._get_subagent_result(call.args), False
        if call.name != "run_bash":
            return tools.execute(self.workspace, call.name, call.args), False
        live = ui.LiveOutput()
        try:
            return tools.execute(self.workspace, call.name, call.args, on_output=live), live.shown
        finally:
            live.close()

    def _run_subagent(self, args: dict) -> dict:
        """Запустить субагента с чистой историей и вернуть родителю лишь его итоговый текст.

        По умолчанию субагент только читает; write=true добавляет write_file/edit_file (без run_bash).
        background=true: субагент работает в потоке, вызов возвращается сразу с id, итог берут get_subagent_result.
        История субагента в историю родителя не попадает; у него свой лог в подкаталоге рядом с логом родителя
        (там его не найдёт find_latest_log). Ctrl+C останавливает и субагента, и родителя.
        """
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "bad arguments for run_subagent: 'prompt' must be a non-empty string"}
        background, write = args.get("background") is True, args.get("write") is True
        if background and write and not self.auto_yes:
            # Подтверждение из фонового потока перебило бы ввод пользователя в основном REPL.
            return {"ok": False, "error": "a background subagent cannot ask the user to confirm writes; run it "
                    "in the foreground (background=false) or start the agent with --yes"}
        self._subagents += 1
        sub = Agent(self.llm, self.workspace, auto_yes=self.auto_yes, max_steps=min(self.max_steps, SUBAGENT_WRITE_MAX_STEPS if write else SUBAGENT_MAX_STEPS),
                    log_dir=self.log_path.parent / f"{self.log_path.stem}-subagents",
                    project=Project(instructions=self.project.instructions),  # CLAUDE.md да, навыки и хуки нет
                    read_only=True, can_write=write, log_name=f"sub{self._subagents:02d}.jsonl",
                    ui_out=ui.Quiet() if background else ui.Nested(self._subagents))
        run = SubRun(self._subagents, prompt, sub, write)
        self.subruns[run.id] = run
        mode = ("запись разрешена" if write else "только чтение") + (", в фоне" if background else "")
        self.ui.info(f"Субагент #{run.id} ({mode}) запущен, лог: {sub.log_path}; просмотр: /agent {run.id}")
        if background:
            run.thread = threading.Thread(target=self._work, args=(run,), name=f"subagent-{run.id}", daemon=True)
            run.thread.start()
            return {"ok": True, "id": run.id, "status": "running",
                    "note": "running in the background; call get_subagent_result with this id when you need the answer"}
        self._work(run)
        if sub.outcome == "cancelled":
            raise KeyboardInterrupt  # родитель закроет ход сам (_stop)
        return self._subrun_result(run)

    @staticmethod
    def _work(run: SubRun) -> None:
        try:
            run.answer = run.agent.run(run.prompt)
        except BaseException as e:  # поток не должен умирать молча: родитель увидит ошибку в результате
            run.answer = f"{type(e).__name__}: {e}"

    @staticmethod
    def _subrun_result(run: SubRun) -> dict:
        extra = {"changed_files": run.agent.changed} if run.write else {}  # родитель должен знать о частичной работе
        if run.status == "done":
            return {"ok": True, "status": "done", "id": run.id, "result": run.answer, **extra}
        return {"ok": False, "id": run.id, "error": f"subagent did not finish ({run.status}): {run.answer}"
                + (f" It already changed these files, re-read them before editing: {run.agent.changed}"
                   if run.agent.changed else ""), **extra}

    def _get_subagent_result(self, args: dict) -> dict:
        """Итог фонового субагента. wait=true (по умолчанию) ждёт завершения; Ctrl+C прерывает ожидание."""
        run = self.subruns.get(args.get("id")) if isinstance(args.get("id"), int) else None
        if run is None:
            return {"ok": False, "error": f"unknown subagent id {args.get('id')!r}; known: {sorted(self.subruns)}"}
        if run.thread and run.thread.is_alive():
            if args.get("wait", True) is False:
                return {"ok": True, "id": run.id, "status": "running"}
            while run.thread.is_alive():
                run.thread.join(0.2)  # короткими кусками, чтобы Ctrl+C сработал сразу
        run.reported = True
        return self._subrun_result(run)

    def _cancel_subagents(self) -> None:
        for run in self.subruns.values():
            if run.status == "running":
                run.agent._cancel.set()

    def _report_finished(self) -> None:
        """Сообщить пользователю о фоновых субагентах, закончивших работу (из потока печатать нельзя)."""
        for run in self.subruns.values():
            if run.thread and not run.reported and run.status != "running":
                run.reported = True
                self.ui.info(f"Субагент #{run.id} завершился ({run.status}); просмотр: /agent {run.id}")

    def list_subagents(self) -> None:
        """/agents: все субагенты этой сессии."""
        if not self.subruns:
            self.ui.info("Субагентов в этой сессии не было.")
        for run in self.subruns.values():
            kind = ("запись" if run.write else "чтение") + (", фон" if run.thread else "")
            self.ui.info(f"#{run.id} [{run.status}] ({kind}) {run.prompt.strip().splitlines()[0][:70]}")

    def show_subagent(self, n: int) -> bool:
        """/agent N: развернуть субагента: его пояснения, вызовы и результаты из лога (идущего можно смотреть частично)."""
        run = self.subruns.get(n)
        if run is None:
            self.ui.error(f"Нет субагента #{n}. Список: /agents")
            return False
        self.ui.info(f"Субагент #{run.id} [{run.status}]: {run.prompt.strip()}")
        for line in run.agent.log_path.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "model" and ev.get("text"):
                self.ui.model_text(ev["text"])
            elif ev.get("event") == "tool":
                self.ui.tool_call(ev["name"], ev["args"])
                self.ui.tool_result(ev["result"])
            elif ev.get("event") in ("llm_error", "max_steps", "loop_stop", "cancelled"):
                self.ui.info(f"[{ev['event']}] {ev.get('error', '')}")
        return True

    def _maybe_auto_compact(self) -> None:
        """Сжать историю перед новой задачей, если последний промпт перевалил порог.

        Только здесь, между задачами: история заканчивается текстом модели, ни один вызов не висит.
        Посреди хода сжимать нельзя: пары вызов/результат и thought signatures пришлось бы рвать.
        """
        if not self.compact_at or not self.history or self.last_prompt is None or self.last_prompt < self.compact_at:
            return
        self.ui.info(f"Автосжатие: промпт {self.last_prompt} токенов >= порога {self.compact_at}.")
        self._log("auto_compact", prompt_tokens=self.last_prompt, threshold=self.compact_at)
        self.compact()  # при сбое сама сообщает об ошибке и оставляет историю как есть

    def _confirm(self, name: str, args: dict) -> bool:
        if self.auto_yes or name not in tools.NEEDS_CONFIRMATION:
            return True
        # ui показывает действие целиком (команду, diff, содержимое): пользователь одобряет именно это.
        return input(self.ui.confirm_request(name, args)).strip().lower() in ("y", "yes", "д", "да")

    def run(self, task: str, files: list[tuple[str, bytes]] | None = None) -> str:
        self.outcome = "failed"  # станет done/cancelled в соответствующих ветках; нужен run_subagent
        try:
            block, context = self._run_hooks("UserPromptSubmit", {"prompt": task})
        except KeyboardInterrupt:  # задача ещё не в истории: отменять нечего
            return self._cancelled_outside_turn()
        if block:  # задача не попадает ни в историю, ни к модели
            msg = f"Задача заблокирована хуком UserPromptSubmit: {block}"
            self._log("task_blocked", task=task, reason=block)
            self.ui.error(msg)
            return msg
        self._maybe_auto_compact()
        self._add(self.llm.user_message(
            task + (f"\n\n[Context from UserPromptSubmit hook]\n{context}" if context else ""), files))
        self._log("task", task=task, files=len(files or []))

        pending: list | None = None  # вызовы хода, для которых tool_results ещё не в истории (нужно при Ctrl+C)
        results: list = []
        last_sig, repeat = None, 0   # защита от зацикливания: сколько раз подряд один и тот же вызов
        stop_hook_used = False       # Stop-хук может вернуть модель к работе только один раз за задачу
        try:
            for step in range(1, self.max_steps + 1):
                if self._cancel.is_set():  # родитель нажал Ctrl+C: фоновый субагент заканчивает как отменённый
                    raise KeyboardInterrupt
                self._report_finished()
                try:
                    schemas = [s for s in self.schemas if self._allowed(s["name"])]
                    reply = self.llm.generate(self.history, self._system(), schemas)
                except Exception as e:  # API не ответил даже после повторов: сообщаем и не роняем REPL
                    msg = f"Вызов LLM не удался: {type(e).__name__}: {e}"
                    self._log("llm_error", step=step, error=msg)
                    self.ui.error(msg)
                    return msg

                self._add(reply.message)  # без изменений: сохраняет thought signatures целыми
                if reply.text:
                    self.ui.model_text(reply.text)  # пояснение к вызову и финальный ответ; main ничего не дублирует
                self.ui.usage(step, self.max_steps, reply.usage)
                self.last_prompt = (reply.usage or {}).get("prompt")
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
                    self.outcome = "done"
                    return reply.text

                pending, results = reply.tool_calls, []
                for call in pending:
                    self.ui.tool_call(call.name, call.args)
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
                    elif not self._allowed(call.name):  # модель могла выдать имя, которого нет в схемах
                        result = {"ok": False, "error": "tool not available in plan mode" if self.plan_mode
                                  else "tool not available: this agent is read-only"}
                    elif block := self._run_hooks("PreToolUse", {"tool_input": call.args}, call.name)[0]:
                        result = {"ok": False, "error": f"blocked by a PreToolUse hook: {block}"}
                    elif self._confirm(call.name, call.args):
                        result, shown = self._execute(call)
                        if call.name in WRITE_TOOLS and result.get("ok") and call.args.get("path") not in self.changed:
                            self.changed.append(call.args.get("path"))
                        feedback = self._run_hooks("PostToolUse", {"tool_input": call.args, "tool_response": result},
                                                   call.name)[0]
                        if feedback:  # хук просит модель обратить внимание на результат
                            result = {**result, "hook_feedback": feedback}
                    else:
                        result = {"ok": False, "error": "user denied this action"}
                    self.ui.tool_result(result, streamed=shown)
                    self._log("tool", step=step, name=call.name, args=call.args, result=result)
                    results.append((call, result))
                self._add(self.llm.tool_results_message(results))
                pending = None

            msg = f"Остановлено: достигнут лимит шагов ({self.max_steps}), финального ответа нет."
            self._log("max_steps", step=self.max_steps)
            self.ui.error(msg)
            return msg
        except KeyboardInterrupt:
            # Ctrl+C: прерываем запрос к модели или инструмент, но историю оставляем валидной.
            self._log("cancelled")
            self.outcome = "cancelled"
            self._cancel_subagents()
            return self._stop(pending or [], results, "cancelled by user", "Прервано пользователем (Ctrl+C).")

    def _cancelled_outside_turn(self) -> str:
        """Ctrl+C там, где история уже валидна (хук до начала задачи или после финального ответа): ничего не закрываем."""
        self._log("cancelled")
        self.outcome = "cancelled"
        msg = "Прервано пользователем (Ctrl+C)."
        self.ui.error(msg)
        return msg

    def _stop(self, pending: list, results: list, reason: str, msg: str) -> str:
        self._close_turn(pending, results, reason, f"[{reason}]")  # для модели: по-английски
        self.ui.error(msg)
        return msg

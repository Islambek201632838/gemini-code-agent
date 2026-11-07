"""Хуки: команды оболочки, которые запускаются в ключевых точках цикла (в духе хуков Claude Code).

События:
    UserPromptSubmit  перед отправкой задачи модели (stdout хука добавляется к задаче как контекст)
    PreToolUse        перед вызовом инструмента, до подтверждения пользователем
    PostToolUse       после выполнения инструмента
    Stop              когда модель дала финальный ответ

Хук получает JSON на stdin (hook_event_name, cwd, tool_name, tool_input, ...) и работает в каталоге workspace.
Код выхода: 0 — всё хорошо; 2 — БЛОКИРОВКА (stderr уходит модели или пользователю);
любой другой код и таймаут — предупреждение пользователю, работа продолжается.

Формат настройки (workspace/.claude/settings.json), matcher — регулярное выражение по имени инструмента:
    {"hooks": {"PreToolUse": [{"matcher": "run_bash|write_file",
                               "hooks": [{"type": "command", "command": "...", "timeout": 10}]}]}}

Модуль ничего не знает ни про LLM, ни про agent.py: он только запускает команды и возвращает результаты.
"""
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
TOOL_EVENTS = ("PreToolUse", "PostToolUse")  # только у них есть matcher
DEFAULT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 10_000
BLOCK_CODE = 2


@dataclass
class Hook:
    event: str
    command: str
    matcher: str = ""
    timeout: int = DEFAULT_TIMEOUT


@dataclass
class HookResult:
    exit_code: int | None   # None: хук убит по таймауту
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def blocked(self) -> bool:
        return self.exit_code == BLOCK_CODE

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def parse(raw) -> tuple[list[Hook], list[str]]:
    """Настройка (значение ключа "hooks") -> список хуков и предупреждения. Ошибочные записи пропускаются."""
    hooks: list[Hook] = []
    warnings: list[str] = []
    if raw is None:
        return hooks, warnings
    if not isinstance(raw, dict):
        return hooks, ['"hooks" в settings.json должен быть объектом {событие: [...]}']
    for event, groups in raw.items():
        if event not in EVENTS:
            warnings.append(f"неизвестное событие хука '{event}' (доступны: {', '.join(EVENTS)})")
            continue
        if not isinstance(groups, list):
            warnings.append(f"hooks.{event} должен быть списком")
            continue
        for group in groups:
            matcher = group.get("matcher", "") if isinstance(group, dict) else None
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(matcher, str) or not isinstance(entries, list):
                warnings.append(f"hooks.{event}: ожидается {{\"matcher\": \"...\", \"hooks\": [...]}}")
                continue
            try:
                re.compile(matcher)
            except re.error as e:
                warnings.append(f"hooks.{event}: неверный matcher {matcher!r}: {e}")
                continue
            for entry in entries:
                command = entry.get("command") if isinstance(entry, dict) else None
                timeout = entry.get("timeout", DEFAULT_TIMEOUT) if isinstance(entry, dict) else None
                if (not isinstance(command, str) or not command.strip() or entry.get("type", "command") != "command"
                        or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
                    warnings.append(f"hooks.{event}: нужна запись {{\"type\": \"command\", \"command\": \"...\"}}")
                    continue
                hooks.append(Hook(event, command, matcher, timeout))
    return hooks, warnings


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # pid == pgid, т.к. запуск с start_new_session=True
    except (ProcessLookupError, PermissionError):
        pass


def run_command(command: str, payload: dict, cwd: Path, timeout: float) -> HookResult:
    """Запустить команду хука: JSON на stdin, своя группа процессов, по таймауту и Ctrl+C убивается вся группа."""
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(cwd)}
    proc = subprocess.Popen(command, shell=True, cwd=cwd, env=env, text=True, errors="replace",
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        try:
            out, err = proc.communicate(json.dumps(payload, ensure_ascii=False, default=str), timeout=timeout)
            return HookResult(proc.returncode, out[:MAX_OUTPUT_CHARS], err[:MAX_OUTPUT_CHARS])
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                out, err = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:  # потомок ушёл из группы и держит каналы: не ждём его
                out, err = "", ""
            return HookResult(None, (out or "")[:MAX_OUTPUT_CHARS], (err or "")[:MAX_OUTPUT_CHARS], timed_out=True)
    except BaseException:  # в том числе KeyboardInterrupt: хук не должен остаться жить
        _kill_group(proc)
        raise


class Hooks:
    def __init__(self, hooks: list[Hook], workspace: Path):
        self.hooks = list(hooks)
        self.workspace = Path(workspace)

    def __bool__(self) -> bool:
        return bool(self.hooks)

    def matching(self, event: str, tool_name: str | None = None) -> list[Hook]:
        result = []
        for h in self.hooks:
            if h.event != event:
                continue
            if event in TOOL_EVENTS and h.matcher not in ("", "*") and not re.fullmatch(h.matcher, tool_name or ""):
                continue
            result.append(h)
        return result

    def run(self, event: str, payload: dict | None = None, tool_name: str | None = None) -> list[tuple[Hook, HookResult]]:
        """Выполнить подходящие хуки по порядку. После первой блокировки (код 2) остальные не запускаются."""
        data = {"hook_event_name": event, "cwd": str(self.workspace), **(payload or {})}
        if tool_name is not None:
            data["tool_name"] = tool_name
        results = []
        for hook in self.matching(event, tool_name):
            res = run_command(hook.command, data, self.workspace, hook.timeout)
            results.append((hook, res))
            if res.blocked:
                break
        return results

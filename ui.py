"""Оформление вывода в терминале (без зависимостей, только ANSI-коды).

Модуль ничего не знает ни про LLM, ни про инструменты: он получает готовые данные
(текст, имя и аргументы вызова, dict результата) и красиво их печатает.
Цвета включаются только в настоящем терминале; их можно отключить переменной NO_COLOR.
"""
import difflib
import json
import os
import re
import sys

_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ

ACCENT = "38;5;173"  # тёплый оранжевый, как у Claude Code
RED, GREEN, CYAN, DIM, BOLD = "31", "32", "36", "2", "1"

PREVIEW_LINES = 6   # сколько строк вывода инструмента показывать
LIVE_LINES = 20     # сколько строк вывода команды показывать вживую
LINE_WIDTH = 110    # длинные строки обрезаем, полный результат всё равно есть в JSONL-логе


def _c(text: str, *codes: str) -> str:
    if not _COLOR or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def _short(text: str, width: int = LINE_WIDTH) -> str:
    text = text.replace("\t", "    ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _markdown(text: str) -> str:
    """Минимальный рендер markdown: **жирный** и `код`."""
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: _c(m.group(1), BOLD), text)
    return re.sub(r"`([^`\n]+)`", lambda m: _c(m.group(1), CYAN), text)


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# ---------------------------------------------------------------- шапка и ввод

def banner(model: str, workspace, log_path) -> None:
    rows = [("✻ Учебный кодинг-агент", ACCENT, BOLD), (f"модель: {model}",), (f"папка:  {workspace}",),
            (f"лог:    {log_path}",)]
    width = max(len(r[0]) for r in rows) + 2
    print(_c("╭" + "─" * (width + 2) + "╮", ACCENT))
    for text, *style in rows:
        pad = " " * (width - len(text))
        print(_c("│ ", ACCENT) + _c(text, *(style or [DIM])) + pad + _c(" │", ACCENT))
    print(_c("╰" + "─" * (width + 2) + "╯", ACCENT))
    print(_c("  Введите задачу. /exit — выход, Ctrl+C — прервать.", DIM))


def prompt() -> str:
    # Без ANSI в строке приглашения: иначе readline неверно считает ширину и ломает редактирование.
    return "\n> "


# ---------------------------------------------------------------- ход агента

def model_text(text: str) -> None:
    print("\n" + _c("●", ACCENT) + " " + _markdown(_indent(text.strip(), "  ")).lstrip())


def usage(step: int, max_steps: int, usage_info: dict) -> None:
    tokens = ", ".join(f"{k} {v}" for k, v in (usage_info or {}).items() if v is not None)
    line = f"шаг {step}/{max_steps}" + (f" · токены: {tokens}" if tokens else "")
    print(_c("  " + line, DIM))


def _args_summary(name: str, args: dict) -> str:
    if name in ("read_file", "write_file", "edit_file") and "path" in args:
        extra = f":{args['offset']}" if args.get("offset") else ""
        return f"{args['path']}{extra}"
    if name == "glob" and "pattern" in args:
        return str(args["pattern"])
    if name == "grep" and "pattern" in args:
        return f"{args['pattern']!r}" + (f" в {args['path']}" if args.get("path") else "")
    if name == "run_bash" and "command" in args:
        return str(args["command"]).splitlines()[0] if args["command"] else ""
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())


def tool_call(name: str, args: dict) -> None:
    print("\n" + _c("●", GREEN) + " " + _c(name, BOLD) + _c(f"({_short(_args_summary(name, args), 90)})", DIM))


class LiveOutput:
    """Живой вывод команды: печатает строки по мере появления, не больше LIVE_LINES.

    Остальное молча считается, а полный (обрезанный до лимита) вывод всё равно уходит модели и в JSONL-лог.
    Вызывается из основного потока: tools.run_bash отдаёт текст через очередь.
    """

    def __init__(self):
        self.shown = False   # напечатали ли хоть что-то (тогда итоговая сводка без повторного превью)
        self._printed = 0
        self._skipped = 0
        self._buf = {"stdout": "", "stderr": ""}

    def __call__(self, stream: str, text: str) -> None:
        *lines, self._buf[stream] = (self._buf[stream] + text).split("\n")
        for line in lines:
            self._line(line)

    def _line(self, line: str) -> None:
        if not line.strip():
            return
        if self._printed >= LIVE_LINES:
            self._skipped += 1
            return
        self.shown = True
        self._printed += 1
        print("    " + _c(_short(line.rstrip().split("\r")[-1]), DIM), flush=True)  # \r: берём последнюю перерисовку

    def close(self) -> None:
        for stream in self._buf:  # последняя строка без перевода строки
            self._line(self._buf[stream])
            self._buf[stream] = ""
        if self._skipped:
            print("    " + _c(f"… ещё строк: {self._skipped}", DIM), flush=True)
            self._skipped = 0


def tool_result(result: dict, streamed: bool = False) -> None:
    """Короткая человекочитаемая сводка вместо сырого JSON. streamed: вывод уже был показан вживую."""
    if not result.get("ok", False) and "error" in result:
        print("  " + _c("⎿ ✗ " + _short(str(result["error"])), RED))
        return

    preview: list[str] = []
    if "content" in result:
        summary = f"прочитано строк: {result.get('total_lines', '?')}"
    elif "files" in result:
        summary = f"найдено файлов: {result.get('count', len(result['files']))}"
        preview = result["files"]
    elif "matches" in result:
        lines = [line for line in str(result["matches"]).splitlines() if line]
        summary = f"найдено совпадений: {len(lines)}" + (" (усечено)" if result.get("truncated") else "")
        preview = lines
    elif "output" in result:
        code = result.get("exit_code")
        summary = "выполнено" if code == 0 else f"завершилось с кодом {code}"
        preview = [line for line in str(result["output"]).splitlines() if line.strip()]
    else:
        summary = str(result.get("message", "готово"))

    failed = "exit_code" in result and not result.get("ok")
    print("  " + _c("⎿ " + ("✗ " if failed else "✓ ") + summary, RED if failed else DIM))
    if streamed:
        return
    for line in preview[:PREVIEW_LINES]:
        print("    " + _c(_short(line), DIM))
    if len(preview) > PREVIEW_LINES:
        print("    " + _c(f"… ещё {len(preview) - PREVIEW_LINES}", DIM))


def info(message: str) -> None:
    print(_c("  " + message, DIM))


def error(message: str) -> None:
    print("\n" + _c("✗ " + message, RED))


# ---------------------------------------------------------------- подтверждение

def confirm_request(name: str, args: dict) -> str:
    """Показать, что именно будет выполнено, и вернуть текст приглашения для input()."""
    print("\n" + _c(f"? Разрешить {name}?", ACCENT, BOLD))
    if name == "run_bash":
        print(_indent(_c(str(args.get("command", "")), CYAN), "  $ "))
    elif name == "edit_file":
        print("  " + _c(str(args.get("path", "")), BOLD))
        old = str(args.get("old_text", "")).splitlines()
        new = str(args.get("new_text", "")).splitlines()
        for line in difflib.ndiff(old, new):
            if line.startswith("- "):
                print("  " + _c("- " + _short(line[2:]), RED))
            elif line.startswith("+ "):
                print("  " + _c("+ " + _short(line[2:]), GREEN))
            elif line.startswith("  "):
                print("  " + _c("  " + _short(line[2:]), DIM))
    elif name == "write_file":
        content = str(args.get("content", ""))
        print("  " + _c(str(args.get("path", "")), BOLD) + _c(f" ({len(content)} символов)", DIM))
        for line in content.splitlines()[:PREVIEW_LINES]:
            print("  " + _c("+ " + _short(line), GREEN))
    else:
        print(_indent(json.dumps(args, ensure_ascii=False, indent=2)))
    return "  Одобрить? [y/N] "

"""Реализации инструментов и их схемы в стиле JSON.

Правила дизайна (ПОЧЕМУ):
- Каждый инструмент возвращает dict: {"ok": True, ...} или {"ok": False, "error": "..."}.
  Инструменты никогда не бросают исключения в цикл агента; модель должна УВИДЕТЬ ошибку, чтобы исправиться.
- Каждый путь проходит через safe_path(), поэтому модель не может выйти за пределы рабочей папки.
- Каждый вывод обрезается, чтобы один огромный файл не затопил контекстное окно.
"""
import re
import subprocess
from pathlib import Path

MAX_OUTPUT_CHARS = 10_000
MAX_RESULTS = 200  # ограничение на число совпадений glob / grep
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}

# Denylist — это НЕ настоящая безопасность (см. README): он лишь останавливает очевидные ошибки.
DENYLIST = [
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME)(\s|$)",  # rm -rf / или ~
    r"\bsudo\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{.*\};\s*:",  # форк-бомба
    r"\b(curl|wget)\b[^|]*\|\s*(sh|bash)\b",  # загрузка с передачей прямо в shell
    r">\s*/dev/sd",
    r"\bshutdown\b|\breboot\b",
]


class ToolError(Exception):
    """Ожидаемый, объяснимый сбой. Преобразуется в {"ok": False, "error": ...}."""


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated: {len(text) - limit} more characters]"


def safe_path(workspace: Path, user_path: str) -> Path:
    """Разрешить user_path внутри workspace или выбросить ToolError.

    resolve() схлопывает '..' И следует по симлинкам, поэтому симлинк, указывающий
    за пределы рабочей папки, разворачивается в реальную цель и отклоняется в relative_to().
    """
    root = workspace.resolve()
    candidate = (root / user_path).resolve()  # абсолютный user_path заменяет root; ловится ниже
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ToolError(f"path '{user_path}' is outside the workspace") from None
    return candidate


def _rel(workspace: Path, p: Path) -> str:
    return str(p.relative_to(workspace.resolve()))


def _walk(base: Path):
    """Выдавать файлы внутри base, пропуская «шумные» каталоги."""
    for p in sorted(base.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            yield p


# ---------------------------------------------------------------- tools

def read_file(workspace, path, offset=None, limit=None):
    p = safe_path(workspace, path)
    if not p.is_file():
        raise ToolError(f"'{path}' is not a file or does not exist")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        raise ToolError(f"'{path}' is not a UTF-8 text file") from None
    start = max(int(offset or 1), 1)  # нумерация строк с 1, как в редакторах
    end = start - 1 + int(limit) if limit else len(lines)
    chunk = lines[start - 1:end]
    body = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start))
    return {"ok": True, "total_lines": len(lines), "content": truncate(body)}


def write_file(workspace, path, content):
    p = safe_path(workspace, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "message": f"wrote {len(content)} characters to {path}"}


def edit_file(workspace, path, old_text, new_text):
    p = safe_path(workspace, path)
    if not p.is_file():
        raise ToolError(f"'{path}' is not a file or does not exist")
    if not old_text:
        raise ToolError("old_text must not be empty")
    text = p.read_text(encoding="utf-8")
    count = text.count(old_text)
    # Ровно одно совпадение: ноль значит, что модель неверно помнит файл,
    # несколько — что правка неоднозначна. Оба случая должны быть явной ошибкой.
    if count == 0:
        raise ToolError("old_text not found; re-read the file and copy the text exactly")
    if count > 1:
        raise ToolError(f"old_text matches {count} places; include more surrounding lines to make it unique")
    p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return {"ok": True, "message": f"edited {path}"}


def glob(workspace, pattern):
    root = workspace.resolve()
    matches = []
    for p in sorted(root.glob(pattern)):
        # Patterns like '../*' or symlinks can point outside: check every match.
        try:
            rel = p.resolve().relative_to(root)
        except ValueError:
            continue
        if p.is_file() and not any(part in SKIP_DIRS for part in rel.parts):
            matches.append(str(rel))
    return {"ok": True, "count": len(matches), "files": matches[:MAX_RESULTS]}


def grep(workspace, pattern, path=None):
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}") from None
    base = safe_path(workspace, path or ".")
    files = [base] if base.is_file() else _walk(base)
    hits = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue  # бинарный или нечитаемый файл: молча пропускаем
        for n, line in enumerate(lines, 1):
            if regex.search(line):
                hits.append(f"{_rel(workspace, f)}:{n}:{line.strip()}")
                if len(hits) >= MAX_RESULTS:
                    return {"ok": True, "truncated": True, "matches": truncate("\n".join(hits))}
    return {"ok": True, "truncated": False, "matches": truncate("\n".join(hits))}


def check_command(command: str):
    """Выбросить ToolError, если команда подпадает под denylist."""
    for pattern in DENYLIST:
        if re.search(pattern, command):
            raise ToolError(f"command blocked by denylist (pattern: {pattern})")


def run_bash(workspace, command, timeout=60):
    check_command(command)
    try:
        r = subprocess.run(
            command, shell=True, cwd=workspace.resolve(), capture_output=True,
            text=True, timeout=int(timeout),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"command timed out after {timeout}s") from None
    out = truncate((r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else ""))
    return {"ok": r.returncode == 0, "exit_code": r.returncode, "output": out}


# ---------------------------------------------------------------- registry

TOOLS = {f.__name__: f for f in (read_file, write_file, edit_file, glob, grep, run_bash)}
NEEDS_CONFIRMATION = {"write_file", "edit_file", "run_bash"}


def execute(workspace: Path, name: str, args: dict) -> dict:
    """Выполнить инструмент по имени; ЛЮБОЙ сбой становится структурированной ошибкой для модели."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown tool '{name}'. Available: {sorted(TOOLS)}"}
    try:
        return fn(workspace, **args)
    except ToolError as e:
        return {"ok": False, "error": str(e)}
    except TypeError as e:  # неверные/отсутствующие аргументы от модели
        return {"ok": False, "error": f"bad arguments for {name}: {e}"}
    except Exception as e:  # крайняя мера: всё равно не роняем цикл
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# Модель выбирает инструменты в основном по этим описаниям, поэтому в них сказано, КОГДА использовать каждый.
SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read a text file from the workspace. Returns numbered lines. "
                       "Use offset/limit (1-based line numbers) to read part of a large file. "
                       "Always read a file before editing it.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace."},
            "offset": {"type": "integer", "description": "First line to read (1-based)."},
            "limit": {"type": "integer", "description": "Maximum number of lines to return."},
        }, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Create a NEW file or completely overwrite an existing one with the given content. "
                       "To change part of an existing file use edit_file instead: it is safer and cheaper.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace."},
            "content": {"type": "string", "description": "Full new content of the file."},
        }, "required": ["path", "content"]},
    },
    {
        "name": "edit_file",
        "description": "Replace exactly one occurrence of old_text with new_text in an existing file. "
                       "Fails if old_text is missing or appears more than once, so include enough "
                       "surrounding lines to make it unique. Prefer this over write_file for small changes.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace."},
            "old_text": {"type": "string", "description": "Exact text to replace (copy it from read_file output, without line numbers)."},
            "new_text": {"type": "string", "description": "Replacement text."},
        }, "required": ["path", "old_text", "new_text"]},
    },
    {
        "name": "glob",
        "description": "Find files by NAME/path pattern (e.g. '**/*.py', 'tests/test_*.py'). "
                       "Use it to discover the project layout. To search file CONTENTS use grep.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern relative to the workspace."},
        }, "required": ["pattern"]},
    },
    {
        "name": "grep",
        "description": "Search file CONTENTS with a regular expression. Returns 'file:line:text' lines (capped). "
                       "Use it to find where a function, variable or error message is used. "
                       "To find files by name use glob.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regular expression."},
            "path": {"type": "string", "description": "Optional file or directory to limit the search."},
        }, "required": ["pattern"]},
    },
    {
        "name": "run_bash",
        "description": "Run a shell command in the workspace directory and return output and exit code. "
                       "Use it to run tests or scripts to verify your changes. "
                       "Do not use it for reading/searching/editing files: use the dedicated tools.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell command to run."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)."},
        }, "required": ["command"]},
    },
]

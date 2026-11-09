"""Вложения в задаче: `@путь/к/файлу` в тексте превращается во вложение, если тип файла поддерживается.

Модуль ничего не знает про Gemini: возвращает пары (mime, bytes), а из них сообщение строит llm.py.
Любой существующий файл можно указать через @; для неподдерживаемого типа возвращается заметка
для пользователя, а токен остаётся в тексте как есть (файл модели не уходит).
Пути с пробелами не поддерживаются (токен кончается на первом пробеле).
"""
import re
from pathlib import Path

# Типы, которые Gemini принимает как inline-данные (список по документации; живой API не проверялся).
MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif",
    ".pdf": "application/pdf",
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".html": "text/html",
    ".xml": "text/xml", ".css": "text/css", ".js": "text/javascript", ".json": "application/json",
    ".py": "text/x-python", ".rtf": "text/rtf",
    ".mp3": "audio/mp3", ".wav": "audio/wav", ".aac": "audio/aac", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".aiff": "audio/aiff",
}
MAX_FILES = 5
MAX_BYTES = 7 * 1024 * 1024        # на файл
MAX_TOTAL_BYTES = 15 * 1024 * 1024  # запрос с inline-данными ограничен ~20 МБ суммарно

TOKEN = re.compile(r"(?<!\S)@(\S+)")
TRAILING = ",.;:!?)]}\"'"  # знаки препинания в конце токена не часть пути


class AttachmentError(Exception):
    pass


def extract(task: str, *base_dirs: Path) -> tuple[str, list[tuple[str, bytes]], list[str]]:
    """Найти в задаче `@файл`, прочитать поддерживаемые. Возвращает (текст, вложения, заметки для пользователя).

    Текст: токен вложения заменён на `[file N: имя]`. Заметки: что приложено и что не поддерживается.
    Относительный путь ищется по очереди в base_dirs (обычно: текущий каталог, workspace).
    `@слово`, которое не файл, остаётся текстом (`@dataclass`, почта). Но `@x.png` без файла и превышение
    лимитов это AttachmentError: задача не должна уйти без вложения молча.
    """
    files: list[tuple[str, bytes]] = []
    notes: list[str] = []

    def load(match: re.Match) -> str:
        raw = match.group(1).rstrip(TRAILING)
        tail = match.group(1)[len(raw):]
        path = Path(raw).expanduser()
        candidates = [path] if path.is_absolute() else [d / path for d in base_dirs]
        found = next((c for c in candidates if c.is_file()), None)
        mime = MIME.get(path.suffix.lower())
        if found is None:
            if mime:  # похоже на вложение, но файла нет
                raise AttachmentError(f"файл не найден: {raw}")
            return match.group(0)
        if mime is None:
            notes.append(f"✗ не поддерживается: {found.name} (тип {found.suffix or 'без расширения'}), "
                         "в модель не отправлен; текст и код агент может прочитать сам (read_file)")
            return match.group(0)
        if len(files) >= MAX_FILES:
            raise AttachmentError(f"слишком много вложений (максимум {MAX_FILES})")
        size = found.stat().st_size
        if size > MAX_BYTES or size + sum(len(d) for _, d in files) > MAX_TOTAL_BYTES:
            raise AttachmentError(f"файл слишком большой (до {MAX_BYTES // 1024 // 1024} МБ, "
                                  f"всего до {MAX_TOTAL_BYTES // 1024 // 1024} МБ): {raw}")
        files.append((mime, found.read_bytes()))
        notes.append(f"✓ приложено: {found.name} ({mime}, {size // 1024} КБ)")
        return f"[file {len(files)}: {found.name}]" + tail

    return TOKEN.sub(load, task), files, notes

"""Контекст проекта, который агент берёт из workspace (по образцу Claude Code).

    CLAUDE.md, .claude/CLAUDE.md        инструкции проекта; попадают в системный промпт
    .claude/skills/<имя>/SKILL.md       навыки: «имя + описание» в системном промпте, тело грузится по запросу
    .claude/settings.json               настройки; сейчас только "hooks" (см. hooks.py)

Всё читается один раз при старте. Файлы, чей реальный путь (после симлинков) ведёт за пределы workspace,
игнорируются: так же, как для инструментов, работает правило «за пределы рабочей папки нельзя».
Модуль ничего не знает про LLM и про цикл агента.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import hooks as hooks_mod

INSTRUCTION_FILES = ("CLAUDE.md", ".claude/CLAUDE.md")
MAX_INSTRUCTIONS_CHARS = 20_000
MAX_SKILL_CHARS = 20_000
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
RESERVED = {"exit", "clear", "compact", "help", "plan"}  # имена встроенных команд: навык не может их занять


@dataclass
class Skill:
    name: str
    description: str
    body: str
    directory: str   # путь к каталогу навыка относительно workspace (модель может читать его файлы read_file'ом)

    def render(self, args: str = "") -> str:
        """Текст для модели: тело навыка с подставленными аргументами ($ARGUMENTS или строка в конце)."""
        body = self.body
        if "$ARGUMENTS" in body:
            body = body.replace("$ARGUMENTS", args)
        elif args:
            body += f"\n\nArguments: {args}"
        return (f"Skill '{self.name}' is already loaded below: follow it, do not call use_skill for it. "
                f"Files of this skill are in {self.directory}/.\n\n{body}")


@dataclass
class Project:
    instructions: str = ""
    skills: dict[str, Skill] = field(default_factory=dict)
    hooks: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def system_prompt(self, base: str) -> str:
        parts = [base]
        if self.skills:
            lines = "\n".join(f"- {s.name}: {s.description}" for s in self.skills.values())
            parts.append("Available skills. When the task matches one, call use_skill with its name and follow the "
                         "returned instructions. The user can also run a skill directly as /<name>.\n" + lines)
        if self.instructions:
            parts.append("Project instructions from CLAUDE.md. They were written by the user: follow them.\n"
                         + self.instructions)
        return "\n\n".join(parts)


def _inside(workspace: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Заголовок '---\\nключ: значение\\n---' в начале файла -> (поля, остальной текст). Без YAML-зависимости."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fields = {}
            for line in lines[1:i]:
                key, sep, value = line.partition(":")
                if sep and key.strip():
                    fields[key.strip().lower()] = value.strip().strip("\"'")
            return fields, "\n".join(lines[i + 1:]).strip()
    return {}, text  # заголовок не закрыт: считаем всё телом


def load_instructions(workspace: Path) -> tuple[str, list[str]]:
    texts, warnings = [], []
    for rel in INSTRUCTION_FILES:
        path = workspace / rel
        if not path.is_file():
            continue
        if not _inside(workspace, path):
            warnings.append(f"{rel} ведёт за пределы workspace и проигнорирован")
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            warnings.append(f"{rel} не прочитан: {e}")
            continue
        if text:
            texts.append(text)
    text = "\n\n".join(texts)
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        warnings.append(f"CLAUDE.md длиннее {MAX_INSTRUCTIONS_CHARS} символов и обрезан")
        text = text[:MAX_INSTRUCTIONS_CHARS]
    return text, warnings


def load_skills(workspace: Path) -> tuple[dict[str, Skill], list[str]]:
    skills: dict[str, Skill] = {}
    warnings: list[str] = []
    root = workspace / ".claude" / "skills"
    if not root.is_dir():
        return skills, warnings
    for skill_file in sorted(root.glob("*/SKILL.md")):
        rel = skill_file.relative_to(workspace).as_posix()
        if not _inside(workspace, skill_file):
            warnings.append(f"{rel} ведёт за пределы workspace и проигнорирован")
            continue
        try:
            fields, body = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            warnings.append(f"{rel} не прочитан: {e}")
            continue
        name = fields.get("name") or skill_file.parent.name
        if not SKILL_NAME.fullmatch(name) or name in RESERVED:
            warnings.append(f"{rel}: недопустимое имя навыка '{name}' (a-z, 0-9, '-'; не имя встроенной команды)")
            continue
        if name in skills:
            warnings.append(f"{rel}: навык '{name}' уже определён, пропущен")
            continue
        if not body:
            warnings.append(f"{rel}: пустой навык пропущен")
            continue
        description = fields.get("description") or body.splitlines()[0].lstrip("# ").strip()
        skills[name] = Skill(name, description, body[:MAX_SKILL_CHARS], skill_file.parent.relative_to(workspace).as_posix())
    return skills, warnings


def load_hooks(workspace: Path) -> tuple[list, list[str]]:
    path = workspace / ".claude" / "settings.json"
    if not path.is_file():
        return [], []
    if not _inside(workspace, path):
        return [], [".claude/settings.json ведёт за пределы workspace и проигнорирован"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], [f".claude/settings.json не прочитан: {e}"]
    if not isinstance(data, dict):
        return [], [".claude/settings.json должен быть JSON-объектом"]
    return hooks_mod.parse(data.get("hooks"))


def load(workspace: Path) -> Project:
    workspace = Path(workspace).resolve()
    instructions, w1 = load_instructions(workspace)
    skills, w2 = load_skills(workspace)
    hooks, w3 = load_hooks(workspace)
    return Project(instructions, skills, hooks, w1 + w2 + w3)

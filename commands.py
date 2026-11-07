"""Слэш-команды REPL: /clear, /compact, /help и вызов навыков как /<имя навыка> [аргументы].

/exit обрабатывает main.py. Всё, что не похоже на команду (например, путь /tmp/x.py), уходит агенту как задача.
"""
import re

import ui
from agent import Agent

COMMAND = re.compile(r"/[a-z][a-z0-9-]*")  # только первое слово; пути вроде /tmp/x.py не совпадают

HELP = [
    ("/clear", "забыть весь разговор и начать с чистого листа"),
    ("/compact [на чём сосредоточиться]", "заменить историю кратким пересказом, чтобы экономить токены"),
    ("/help", "эта справка"),
    ("/exit", "выйти"),
]


def handle(agent: Agent, line: str) -> bool:
    """Выполнить слэш-команду. False: это не команда, строку надо отправить агенту как задачу."""
    name, _, rest = line.partition(" ")
    if not COMMAND.fullmatch(name):
        return False
    rest = rest.strip()
    if name == "/clear":
        agent.clear()
        ui.info("Разговор очищен.")
    elif name == "/compact":
        agent.compact(rest)
    elif name == "/help":
        _help(agent)
    elif name[1:] in agent.project.skills:
        agent.run(agent.project.skills[name[1:]].render(rest))
    else:
        ui.error(f"Неизвестная команда {name}. Список: /help")
    return True


def _help(agent: Agent) -> None:
    for cmd, text in HELP:
        ui.info(f"{cmd:<34} {text}")
    for skill in agent.project.skills.values():
        ui.info(f"{'/' + skill.name + ' [аргументы]':<34} навык: {skill.description}")
    if agent.hooks:
        ui.info(f"Хуков активно: {len(agent.hooks.hooks)}")

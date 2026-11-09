"""Интерактивный CLI: введите задачу, наблюдайте за работой агента, /exit для выхода."""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import attachments
import project as project_mod
import ui
from hooks import Hooks

load_dotenv()  # читает GEMINI_API_KEY / GEMINI_MODEL из .env


def confirm_hooks(hooks, auto_yes: bool):
    """Хуки из workspace — это произвольные команды оболочки: показываем их и спрашиваем, как и для run_bash."""
    if not hooks or auto_yes:
        return hooks
    print("В workspace настроены хуки (команды выполнятся на вашей машине):")
    for h in hooks.hooks:
        print(f"  {h.event}" + (f" [{h.matcher}]" if h.matcher else "") + f": {h.command}")
    try:
        answer = input("Включить хуки? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in ("y", "yes", "д", "да"):
        return hooks
    ui.info("Хуки отключены на эту сессию.")
    return Hooks([], hooks.workspace)


def main():
    ap = argparse.ArgumentParser(description="Маленький кодинг-агент")
    ap.add_argument("--workspace", default="./workspace", help="каталог, с которым агенту разрешено работать")
    ap.add_argument("--yes", action="store_true", help="автоматически одобрять write/edit/bash (осторожно!)")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--resume", nargs="?", const="latest", metavar="ЛОГ",
                    help="продолжить прерванную сессию из JSONL-лога (без значения: самый свежий в logs/)")
    args = ap.parse_args()

    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    from agent import Agent, find_latest_log  # импортируем поздно, чтобы --help работал без API-ключа
    from commands import handle
    from llm import LLM
    try:
        llm = LLM()
    except KeyError:
        sys.exit("GEMINI_API_KEY не задан. Скопируйте .env.example в .env и заполните его.")

    proj = project_mod.load(workspace)  # CLAUDE.md, навыки и хуки из workspace
    for w in proj.warnings:
        ui.error(w)
    hooks = confirm_hooks(Hooks(proj.hooks, workspace.resolve()), args.yes)
    try:  # порог автосжатия в токенах prompt; не задан или 0: выключено
        compact_at = int(os.environ.get("GEMINI_COMPACT_TOKENS") or 0)
    except ValueError:
        sys.exit("GEMINI_COMPACT_TOKENS должен быть целым числом (токенов).")
    agent = Agent(llm, workspace, auto_yes=args.yes, max_steps=args.max_steps, project=proj, hooks=hooks,
                  compact_at=compact_at)
    resumed = None
    if args.resume:
        log = find_latest_log() if args.resume == "latest" else Path(args.resume)
        if log is None or not log.is_file():
            sys.exit("Нет лога для восстановления (нужен лог, записанный этой версией: с событиями message).")
        try:
            resumed = (agent.resume(log), log)
        except ValueError as e:
            sys.exit(str(e))
    context = ", ".join(filter(None, [
        "CLAUDE.md" if proj.instructions else "",
        f"навыков: {len(proj.skills)}" if proj.skills else "",
        f"хуков: {len(hooks.hooks)}" if hooks else ""]))
    ui.banner(llm.model, agent.workspace, agent.log_path, context)
    if resumed:
        ui.info(f"Сессия восстановлена: {resumed[0]} сообщений из {resumed[1]}")
    while True:
        try:
            task = input(ui.prompt(agent.plan_mode)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if task == "/exit":
            break
        if task.startswith("/") and handle(agent, task):
            continue
        if task:
            try:  # @картинка.png в тексте становится вложением
                task, files, notes = attachments.extract(task, Path.cwd(), agent.workspace)
            except attachments.AttachmentError as e:
                ui.error(str(e))
                continue
            for note in notes:
                (ui.error if note.startswith("✗") else ui.info)(note)
            agent.run(task, files)  # ответ и ошибки печатает сам агент через ui


if __name__ == "__main__":
    main()

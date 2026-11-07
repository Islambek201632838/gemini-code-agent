"""Интерактивный CLI: введите задачу, наблюдайте за работой агента, /exit для выхода."""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

import ui

load_dotenv()  # читает GEMINI_API_KEY / GEMINI_MODEL из .env


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
    from llm import LLM
    try:
        llm = LLM()
    except KeyError:
        sys.exit("GEMINI_API_KEY не задан. Скопируйте .env.example в .env и заполните его.")

    agent = Agent(llm, workspace, auto_yes=args.yes, max_steps=args.max_steps)
    resumed = None
    if args.resume:
        log = find_latest_log() if args.resume == "latest" else Path(args.resume)
        if log is None or not log.is_file():
            sys.exit("Нет лога для восстановления (нужен лог, записанный этой версией: с событиями message).")
        try:
            resumed = (agent.resume(log), log)
        except ValueError as e:
            sys.exit(str(e))
    ui.banner(llm.model, agent.workspace, agent.log_path)
    if resumed:
        ui.info(f"Сессия восстановлена: {resumed[0]} сообщений из {resumed[1]}")
    while True:
        try:
            task = input(ui.prompt()).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if task == "/exit":
            break
        if task:
            agent.run(task)  # ответ и ошибки печатает сам агент через ui


if __name__ == "__main__":
    main()

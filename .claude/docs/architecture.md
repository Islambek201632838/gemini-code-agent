# Архитектура

Учебный кодинг-агент: цикл «модель → вызовы инструментов → результаты → модель» написан вручную. Фреймворков нет. 

## Карта модулей

```
main.py ──> agent.py ──> llm.py   (единственное место, знающее про Gemini)
   │             ├─────> tools.py (инструменты, схемы, защита путей)
   └─────────────┴─────> ui.py    (оформление вывода, только ANSI)
tests/test_tools.py      workspace/ (демо-проект)      logs/ (JSONL)
```

| Файл | Ответственность | Что НЕ должен знать |
|---|---|---|
| `main.py` | argparse, REPL, `/exit`, загрузка `.env` | про формат сообщений модели |
| `agent.py` | цикл, подтверждения, JSONL-лог (консольный вывод делегирует `ui.py`), лимит шагов | про Gemini и его типы |
| `llm.py` | вызов API, retry/backoff, сборка сообщений | про инструменты (получает только схемы) |
| `ui.py` | баннер, трейс шагов, сводки результатов вместо сырого JSON, diff в подтверждении; цвета только в TTY, отключаются `NO_COLOR` | про LLM и про `tools` |
| `tools.py` | 6 инструментов, `SCHEMAS`, `safe_path`, усечение, денилист | про LLM и про подтверждения |

## Цикл агента (`Agent.run` в `agent.py`)

```
history += user_message(task)
for step in 1..max_steps:
    reply = llm.generate(history, SYSTEM_PROMPT, tools.SCHEMAS)
    history += reply.message              # как есть, без пересборки
    if not reply.tool_calls: return reply.text
    for call in reply.tool_calls:
        result = execute(call) если подтверждено, иначе {"ok": False, "error": "user denied this action"}
    history += tool_results_message(results)   # ВСЕ результаты хода одним сообщением
return "достигнут лимит шагов"
```

История (`Agent.history`) живёт всю сессию REPL, между задачами не сбрасывается и не сжимается.

## Контракт `llm.py` (менять провайдера = переписать только этот файл)

- `LLM(model=None)`: модель берётся из аргумента, затем `GEMINI_MODEL`, затем `DEFAULT_MODEL` (`gemini-3.5-flash`).
- `LLM.user_message(text)` и `LLM.tool_results_message([(ToolCall, result_dict), ...])` строят элементы истории.
- `LLM.generate(history, system, tool_schemas) -> Reply`, где `Reply = (text, tool_calls: list[ToolCall], usage: dict, message)`.
- `message` непрозрачный для `agent.py`: это нативный объект провайдера, который просто кладётся в историю.
- Автоматический вызов функций отключён (`AutomaticFunctionCallingConfig(disable=True)`), иначе SDK исполнял бы цикл сам.
- Retry: ошибки API с кодами 429/500/502/503/504, до 5 повторов, пауза `2**n * 2` с (максимум 60) плюс джиттер. Остальные ошибки (например, 404) пробрасываются сразу, `Agent.run` превращает их в текст и не роняет REPL.

## Контракт `tools.py`

- Каждый инструмент: `fn(workspace: Path, **args) -> dict`. Успех `{"ok": True, ...}`, ошибка `{"ok": False, "error": "..."}`.
- Ожидаемые сбои бросают `ToolError`; `execute()` перехватывает всё (включая `TypeError` от неверных аргументов и любые `Exception`) и всегда возвращает dict. Цикл из-за инструмента не падает.
- Любой путь проходит через `safe_path`: `resolve()` схлопывает `..` и раскрывает симлинки, потом `relative_to(workspace)`. `glob` дополнительно проверяет каждое совпадение (шаблоны вроде `../*`).
- Вывод усечён до 10 000 символов с пометкой `truncated`; `glob`/`grep` ограничены 200 совпадениями.
- `NEEDS_CONFIRMATION = {write_file, edit_file, run_bash}`: подтверждение запрашивает `agent.py`, не сами инструменты.
- `SCHEMAS`: JSON-схемы и описания для модели; они объясняют, когда `grep` а когда `glob`, когда `edit_file` а когда `write_file`.

### Как добавить инструмент
1. Функция в `tools.py` с сигнатурой `(workspace, ...)`, возвращает dict; ожидаемые ошибки через `raise ToolError(...)`.
2. Добавить её в `TOOLS` и запись в `SCHEMAS` (описание на английском, с указанием «когда использовать»).
3. Если инструмент меняет файлы или выполняет код, добавить в `NEEDS_CONFIRMATION`.
4. Тесты в `tests/test_tools.py`.

## Инварианты (не ломать)

1. **Сообщение модели в историю кладётся без изменений.** У моделей Gemini 3 в нём лежат thought signatures; пересборка из текста их теряет.
2. **Все результаты вызовов одного хода идут одним сообщением**, в порядке вызовов.
3. **Ошибка инструмента это обычный результат**, а не исключение: модель должна её увидеть и исправиться.
4. **Тексты ошибок в `tools.py` на английском**: тесты проверяют подстроки (`outside the workspace`, `not found`, `2 places`, `denylist`). Меняете текст, меняйте тест.
5. **Модель не хардкодится**; ключ из `.env` не печатается и не коммитится.
6. **Демо-проект `workspace/` не требует падающего теста.** Изначально в нём был баг в `median` (`test_median_even`); по решению пользователя 2026-11-07 баг исправлен и обратно не возвращается. Нужна учебная задача на исправление: сами внесите баг в `workspace/stats.py`.

## Формат JSONL-лога (`logs/run-<время>.jsonl`)

Одна строка на событие, поле `event`:
- `task`: `{task}`
- `model`: `{step, text, usage, tool_calls: [{name, args}]}`
- `tool`: `{step, name, args, result}` (результат полный, без усечения трейса)
- `llm_error`: `{step, error}`
- `max_steps`: `{step}`

## Команды

```bash
.venv/bin/python -m unittest discover tests            # unit-тесты
.venv/bin/python main.py --workspace ./workspace        # запуск (флаги: --yes, --max-steps N)
cd workspace && ../.venv/bin/python -m unittest         # тесты демо-проекта
```

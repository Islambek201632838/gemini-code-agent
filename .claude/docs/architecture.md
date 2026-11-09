# Архитектура

Учебный кодинг-агент: цикл «модель → вызовы инструментов → результаты → модель» написан вручную. Фреймворков нет. 

## Карта модулей

```
main.py ──> commands.py ─┐
   │                     ▼
   └──────> agent.py ──> llm.py   (единственное место, знающее про Gemini)
               ├───────> tools.py ──> project.py (CLAUDE.md, навыки, настройки)
               ├───────> hooks.py  <── project.py (разбор "hooks")
               └───────> ui.py    (оформление вывода, только ANSI)
tests/                   workspace/ (демо-проект + .claude/)      logs/ (JSONL)
```

| Файл | Ответственность | Что НЕ должен знать |
|---|---|---|
| `main.py` | argparse, REPL, `/exit`, загрузка `.env`, подтверждение хуков при старте | про формат сообщений модели |
| `attachments.py` | разбор `@файл.png` в задаче, чтение файлов (png/jpg/webp, до 5 шт., до 7 МБ) | про Gemini и про цикл |
| `commands.py` | слэш-команды `/clear`, `/compact`, `/plan`, `/help`, `/<навык>`; всё, что не похоже на команду, возвращает как задачу | про Gemini |
| `project.py` | чтение `CLAUDE.md`, `.claude/skills/*/SKILL.md`, `.claude/settings.json`; сборка системного промпта | про LLM и про цикл |
| `hooks.py` | разбор настроек хуков, запуск команд хуков (группа процессов, таймаут) | про LLM и про `agent.py` |
| `agent.py` | цикл, подтверждения, JSONL-лог (консольный вывод делегирует `ui.py`), лимит шагов, вызов хуков, `clear()` / `compact()`, режим плана, автосжатие, `run_subagent` | про Gemini и его типы |
| `llm.py` | вызов API, retry/backoff, сборка сообщений, `summarize()` для `/compact` | про инструменты (получает только схемы) |
| `ui.py` | баннер, трейс шагов, сводки результатов вместо сырого JSON, diff в подтверждении; цвета только в TTY, отключаются `NO_COLOR` | про LLM и про `tools` |
| `tools.py` | 7 инструментов (в том числе `use_skill`), `SCHEMAS`, `safe_path`, усечение, денилист | про LLM и про подтверждения |

## Цикл агента (`Agent.run` в `agent.py`)

```
хук UserPromptSubmit: код 2 -> задача не уходит модели; stdout -> добавляется к задаче
history += user_message(task)
for step in 1..max_steps:
    reply = llm.generate(history, self.system, self.schemas)   # system = SYSTEM_PROMPT + навыки + CLAUDE.md
    history += reply.message              # как есть, без пересборки
    if not reply.tool_calls:
        хук Stop: код 2 (один раз за задачу) -> stderr как сообщение пользователя, цикл продолжается
        return reply.text
    for call in reply.tool_calls:
        повтор >= 5 подряд (то же имя и аргументы) -> остановить цикл
        повтор >= 3 -> не выполнять, вернуть ошибку «NOT run again; change your approach»
        хук PreToolUse: код 2 -> не выполнять и не спрашивать, вернуть {"ok": False, "error": "blocked by a PreToolUse hook: ..."}
        result = execute(call) если подтверждено, иначе {"ok": False, "error": "user denied this action"}
        хук PostToolUse: код 2 -> в result добавляется "hook_feedback": stderr
    history += tool_results_message(results)   # ВСЕ результаты хода одним сообщением
return "достигнут лимит шагов"
# KeyboardInterrupt в любой точке -> _close_turn: результат "cancelled by user" на каждый вызов без ответа
#   + служебное сообщение модели; история остаётся валидной, REPL продолжает работать.
```

Перед вызовами инструментов модель должна писать короткое пояснение (требование в `SYSTEM_PROMPT`); `ui.model_text` печатает его до строки вызова.
Все сообщения истории (`_add`) дополнительно пишутся в лог в исходном виде: из них `Agent.resume(path)` (`main.py --resume`) восстанавливает `history`.

История (`Agent.history`) живёт всю сессию REPL и между задачами не сбрасывается сама. Управляют ей команды:
- `/clear` → `Agent.clear()`: `history = []`, в лог пишется `reset`.
- `/compact [текст]` → `Agent.compact()`: `llm.summarize(history, текст)` присылает пересказ, история становится `[user: пересказ, model: подтверждение]` (роли чередуются), в лог `reset` и два `message`. Пока модель не ответила, история не трогается: ошибка API и Ctrl+C её не портят. Автосжатие: если задан `compact_at` (`GEMINI_COMPACT_TOKENS`) и `Agent.last_prompt` (`usage.prompt` последнего ответа) >= порога, `run()` вызывает `compact()` в самом начале новой задачи, до добавления сообщения пользователя. Только там история заканчивается ответом модели без висящих вызовов; посреди хода сжимать нельзя. Сбой `compact()` задачу не прерывает. `clear`/`compact` сбрасывают `last_prompt`; после `--resume` он неизвестен, пока модель не ответит.

## Режим планирования и субагенты

- `/plan` → `Agent.set_plan_mode(True)` (лог: `mode`). Пока режим включён, `generate` получает только схемы из `tools.READ_ONLY_TOOLS` (`read_file`, `glob`, `grep`; `run_bash` и `use_skill` исключены, они могут писать) и системный промпт с `PLAN_SUFFIX`. Если модель всё равно назвала другой инструмент, цикл возвращает ошибку-результат `tool not available in plan mode` до хуков и подтверждения. `/plan off` отключает режим; `self.system` не меняется, добавка клеится при каждом запросе.
- `run_subagent(prompt)`: схема есть в `tools.SCHEMAS`, но функции в `TOOLS` нет. `Agent._execute` перехватывает вызов (как `run_bash`), потому что нужен доступ к `LLM`, а `tools.py` его не знает. Создаётся `Agent(read_only=True)` с пустой историей, тем же `LLM`, `CLAUDE.md` без навыков и хуков, лимитом `SUBAGENT_MAX_STEPS`=12 и логом `<лог родителя>-subagents/subNN.jsonl`. Родитель получает `{ok: true, result: итоговый текст}` либо `{ok: false, error}`, если субагент не закончил (`Agent.outcome` != `done`). `outcome == cancelled` (Ctrl+C) пробрасывается как `KeyboardInterrupt`, и родитель закрывает ход обычным путём. Рекурсии нет: у `read_only`-агента `run_subagent` не входит в разрешённые инструменты. В режиме плана `run_subagent` тоже скрыт.

## Вложения в задаче

`main.py` передаёт строку в `attachments.extract(task, cwd, workspace)` -> `(текст, файлы, заметки)`. Токен `@путь` к существующему файлу поддерживаемого типа (таблица `MIME`, по документации Gemini; живой API не проверялся) заменяется на `[file N: имя]`, файл уходит модели. Неподдерживаемый тип: заметка `✗ не поддерживается`, токен остаётся текстом, файл не отправляется. `@слово`, не являющееся файлом, остаётся текстом; `@x.png` без файла, превышение лимитов (5 файлов, 7 МБ на файл, 15 МБ всего) это `AttachmentError`: задача не уходит. Заметки печатает `main.py`, затем `Agent.run(task, files)`. В лог вложения попадают в base64 внутри события `message` (нужно для `--resume`), в событии `task` только их число. `/compact` в пересказе упоминает вложение, но сам файл не передаёт.

## Контекст проекта (`project.py`)

Читается один раз при старте из workspace; файлы, чей реальный путь выходит за workspace (симлинк), игнорируются с предупреждением.

- **`CLAUDE.md`** и `.claude/CLAUDE.md` (до 20 000 символов) целиком добавляются в системный промпт. Импортов `@файл` нет.
- **Навыки** `.claude/skills/<имя>/SKILL.md` с заголовком `name` / `description` (имя: `a-z0-9-`, не `exit`/`clear`/`compact`/`help`/`plan`). В системный промпт попадает только список «имя: описание». Тело модель получает инструментом `use_skill(name)` (он не требует подтверждения и показывается модели, только если навыки есть) либо пользователь запускает навык командой `/<имя> [аргументы]`: тело уходит как задача, `$ARGUMENTS` заменяется аргументами (если его нет, они дописываются в конец).
- **`.claude/settings.json`**: ключ `hooks` (см. ниже), остальные ключи игнорируются.

## Хуки (`hooks.py`)

```json
{"hooks": {"PreToolUse": [{"matcher": "run_bash|write_file",
                           "hooks": [{"type": "command", "command": "python3 .claude/hooks/x.py", "timeout": 10}]}]}}
```

События: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`. `matcher` (только у Pre/PostToolUse) это регулярное выражение, которое должно совпасть с именем инструмента целиком; пусто или `*` значит «любой».
Команда запускается через оболочку в каталоге workspace с JSON на stdin (`hook_event_name`, `cwd`, `tool_name`, `tool_input`, `tool_response`, `prompt`, `last_assistant_message`) и переменной `CLAUDE_PROJECT_DIR`; по умолчанию таймаут 30 с. Запуск как у `run_bash`: своя группа процессов, по таймауту и Ctrl+C убивается вся группа.

| Код выхода | Смысл |
|---|---|
| 0 | всё хорошо; stdout показывается пользователю (для `UserPromptSubmit` ещё и добавляется к задаче) |
| 2 | блокировка, stderr читает модель (`PreToolUse`, `PostToolUse`, `Stop`) или пользователь (`UserPromptSubmit`) |
| другой, таймаут | предупреждение пользователю, работа продолжается |

Безопасность: хуки это произвольные команды. `main.py` показывает их при старте и спрашивает `[y/N]` (с `--yes` не спрашивает); при отказе хуки выключены на всю сессию. Конфигурация читается один раз, поэтому правка `settings.json` самим агентом посреди сессии на неё не влияет (демо-хук в `workspace/.claude/hooks/protect_config.py` дополнительно запрещает агенту её менять). Хуки не проходят через `safe_path` и денилист: они исполняются с правами пользователя.

## Контракт `llm.py` (менять провайдера = переписать только этот файл)

- `LLM(model=None)`: модель берётся из аргумента, затем `GEMINI_MODEL`, затем `DEFAULT_MODEL` (`gemini-3.5-flash`).
- `LLM.user_message(text, files=None)` (`files`: вложения, пары `(mime, bytes)`, идут после текста), `LLM.model_message(text)` (служебное сообщение модели, сохраняет чередование ролей) и `LLM.tool_results_message([(ToolCall, result_dict), ...])` строят элементы истории.
- `LLM.dump_message(msg) -> dict` / `LLM.load_message(dict)`: сериализация для лога и `--resume`; bytes (thought signatures) кодируются в base64 без потерь (тест `tests/test_llm.py`). `LLM.unanswered_calls(history)` находит вызовы без результатов в оборванной сессии.
- `LLM.summarize(history, instructions="") -> str`: пересказ истории для `/compact`. История переводится в обычный текст (вызовы инструментов пересказаны, результаты усечены до 1500 символов), поэтому thought signatures и function_call не нужны; инструментов в запросе нет.
- `LLM.generate(history, system, tool_schemas) -> Reply`, где `Reply = (text, tool_calls: list[ToolCall], usage: dict, message)`.
- `message` непрозрачный для `agent.py`: это нативный объект провайдера, который просто кладётся в историю.
- Автоматический вызов функций отключён (`AutomaticFunctionCallingConfig(disable=True)`), иначе SDK исполнял бы цикл сам.
- Retry: ошибки API с кодами 429/500/502/503/504, до 5 повторов, пауза `2**n * 2` с (максимум 60) плюс джиттер. Остальные ошибки (например, 404) пробрасываются сразу, `Agent.run` превращает их в текст и не роняет REPL.

## Контракт `tools.py`

- Каждый инструмент: `fn(workspace: Path, **args) -> dict`. Успех `{"ok": True, ...}`, ошибка `{"ok": False, "error": "..."}`.
- Ожидаемые сбои бросают `ToolError`; `execute()` перехватывает всё (включая `TypeError` от неверных аргументов и любые `Exception`) и всегда возвращает dict. Цикл из-за инструмента не падает.
- Любой путь проходит через `safe_path`: `resolve()` схлопывает `..` и раскрывает симлинки, потом `relative_to(workspace)`. `glob` дополнительно проверяет каждое совпадение (шаблоны вроде `../*`).
- Вывод усечён до 10 000 символов с пометкой `truncated`; `glob`/`grep` ограничены 200 совпадениями.
- `run_bash` (`Popen`): `start_new_session=True` + `os.killpg` по таймауту и при любом исключении (включая `KeyboardInterrupt`); `stdin=DEVNULL`; stdout/stderr читаются потоками через очередь, лимит 10 000 символов на поток применяется *во время* чтения (`_Capture`); колбэк `on_output(stream, text)` вызывается в основном потоке. По таймауту возвращается `{"ok": False, "error": "...timed out...", "output": <частичный вывод>}`.
- `execute(..., on_output=None)` передаёт колбэк только инструментам из `STREAMING`; `KeyboardInterrupt` не перехватывается (его обрабатывает цикл).
- `NEEDS_CONFIRMATION = {write_file, edit_file, run_bash}`: подтверждение запрашивает `agent.py`, не сами инструменты.
- `SCHEMAS`: JSON-схемы и описания для модели; они объясняют, когда `grep` а когда `glob`, когда `edit_file` а когда `write_file`.

`use_skill` читает навыки с диска при каждом вызове (`project.load_skills`), а не из снимка при старте.

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
6. **`/clear` и `/compact` пишут `reset` в лог**, иначе `--resume` воскресит забытый разговор. Историю заменяет только успешный ответ модели.
7. **Демо-проект `workspace/` не требует падающего теста.** Изначально в нём был баг в `median` (`test_median_even`); по решению пользователя 2026-11-07 баг исправлен и обратно не возвращается. Нужна учебная задача на исправление: сами внесите баг в `workspace/stats.py`.

## Формат JSONL-лога (`logs/run-<время>.jsonl`)

Одна строка на событие, поле `event`:
- `task`: `{task}`
- `message`: `{data}`, сообщение истории целиком (`LLM.dump_message`); по этим событиям работает `--resume`. Логи без них (старый формат) восстановить нельзя
- `model`: `{step, text, usage, tool_calls: [{name, args}]}`
- `tool`: `{step, name, args, result}` (результат полный, без усечения трейса)
- `llm_error`: `{step, error}`
- `max_steps`: `{step}`
- `loop_stop`: `{step, name, args}` (пять одинаковых вызовов подряд)
- `cancelled`: Ctrl+C во время задачи
- `mode`: `{plan: true | false}` (`/plan`, `/plan off`)
- `auto_compact`: `{prompt_tokens, threshold}` (перед автосжатием; следом идёт обычный `reset`)
- `reset`: `{reason: "clear" | "compact"}`; `--resume` выбрасывает все `message` до этой строки
- `hook`: `{hook_event, command, exit_code, timed_out, stdout, stderr}`
- `task_blocked`: `{task, reason}` (хук `UserPromptSubmit` отклонил задачу)

## Команды

```bash
.venv/bin/python -m unittest discover tests            # unit-тесты
.venv/bin/python main.py --workspace ./workspace        # запуск (флаги: --yes, --max-steps N, --resume [лог])
cd workspace && ../.venv/bin/python -m unittest         # тесты демо-проекта
```

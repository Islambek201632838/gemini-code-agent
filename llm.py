"""Тонкая обёртка над Gemini API: ЕДИНСТВЕННЫЙ файл, который знает о Gemini.

Чтобы сменить провайдера (например, на Claude API), перепишите этот файл и сохраните
тот же интерфейс: LLM.generate(), LLM.user_message(), LLM.tool_results_message().
agent.py считает сообщения истории непрозрачными объектами и никогда не заглядывает внутрь.
"""
import os
import random
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import errors, types

DEFAULT_MODEL = "gemini-3.5-flash"
RETRY_STATUS = {429, 500, 502, 503, 504}  # лимит запросов / перегрузка: стоит повторить
MAX_RETRIES = 5


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class Reply:
    text: str                                   # текстовая часть ответа (может быть пустой)
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)   # число токенов, если API его вернул
    message: object = None                      # «родное» сообщение провайдера, добавляется в историю


class LLM:
    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    # --- конструкторы сообщений истории (форматы конкретного провайдера живут здесь) ---

    def user_message(self, text: str):
        return types.Content(role="user", parts=[types.Part(text=text)])

    def tool_results_message(self, results: list[tuple[ToolCall, dict]]):
        """Все результаты одного хода модели попадают в ОДНО пользовательское сообщение."""
        parts = [
            types.Part.from_function_response(name=call.name, response=result)
            for call, result in results
        ]
        return types.Content(role="user", parts=parts)

    # --- сам вызов ---

    def generate(self, history: list, system: str, tool_schemas: list[dict]) -> Reply:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name=s["name"], description=s["description"],
                    parameters_json_schema=s["parameters"],
                ) for s in tool_schemas
            ])],
            # Цикл мы запускаем сами; без этого SDK вызывал бы инструменты за нас.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._with_retry(
            lambda: self.client.models.generate_content(
                model=self.model, contents=history, config=config)
        )
        if not response.candidates or not response.candidates[0].content:
            reason = response.candidates[0].finish_reason if response.candidates else "no candidates"
            raise RuntimeError(f"model returned no content (finish reason: {reason})")

        text = "".join(p.text for p in response.candidates[0].content.parts
                       if p.text and not p.thought)
        calls = [ToolCall(name=c.name, args=dict(c.args or {}), id=c.id)
                 for c in (response.function_calls or [])]
        u = response.usage_metadata
        usage = {"prompt": u.prompt_token_count, "output": u.candidates_token_count,
                 "total": u.total_token_count} if u else {}
        # ПОЧЕМУ мы храним «родной» объект content: Gemini 3 прикрепляет к нему
        # "thought signatures", и их нужно вернуть без изменений в следующем запросе.
        return Reply(text=text, tool_calls=calls, usage=usage, message=response.candidates[0].content)

    @staticmethod
    def _with_retry(fn):
        """Экспоненциальная задержка с джиттером; на бесплатном тарифе 429/503 бывают часто."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return fn()
            except errors.APIError as e:
                if e.code not in RETRY_STATUS or attempt == MAX_RETRIES:
                    raise
                delay = min(2 ** attempt * 2, 60) + random.uniform(0, 1)
                print(f"  [ошибка API {e.code}; повтор {attempt + 1}/{MAX_RETRIES} через {delay:.0f}с]")
                time.sleep(delay)

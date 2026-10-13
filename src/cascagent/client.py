"""Транспортный клиент LLM — стриминг через OpenAI-совместимый API (docs/stack.md §4–5).

Реализует транспортный контракт, зафиксированный 2026-10-13 (roadmap.md §9.4,
plugins.md §4.2): стык LLM внутри ``Agent.run`` — это **итератор чанков**,
либо простой итерируемый объект, либо ``callable(messages) -> iterator[str]``.
``LlamaCppClient.stream()`` — ровно такой вызываемый объект: он выдаёт сырые
текстовые дельты (SSE ``delta.content``), по одному чанку на событие сервера,
и завершается строкой-сентинелом ``"[DONE]"``, которую потребляет цикл агента.
Клиент никогда не возвращает готовую строцу целиком; разделение на
think/final — тоже НЕ его задача — поток структурирует встроенный
``ResponseParser`` (plugins.md §3.1).

Соблюдаемые инварианты бэкенда (stack.md §4.3):
1. OpenAI-совместимый эндпоинт (по умолчанию llama.cpp ``:8080``; ADR-001);
2. стабильный префикс SYSTEM передаёт вызывающая сторона (protocol §2) —
   клиент отправляет сообщения дословно, ничего не переписывая;
3. переключатель think на уровне запроса (§5.3): llama.cpp понимает
   параметр тела ``"think": bool``; запасной вариант TabbyAPI/chatml
   использует софт-промпт ``/no_think``, приклеиваемый к ПОСЛЕДНЕМУ
   сообщению пользователя;
4. single-flight — один HTTP-запрос на вызов, без батчинга и прогрева
   (AGENTS.md §9);
5. температура по умолчанию 0.2 (config.md §4 [llm]).

Транспорт инъектируемый (roadmap.md §10 Фаза 2 «transport инъектируем»):
конструктор принимает объект ``session`` с методом ``post(url, json=...,
stream=..., timeout=...)`` — в реальной работе передаётся ``requests.Session()``,
в тестах — фейк (MockLLM подаёт данные агенту напрямую, поэтому этот модуль
не имеет тестов, зависимых от сети).

Граница «CPU-тонкий слой» (roadmap.md §1 п.1): модуль работает только с HTTP;
никакого SQLite и никаких эвристик парсинга здесь нет.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

#: Сентинел, выдаваемый после последней дельты — соответствует проверке
#: в цикле агента (memory.py run(): `if chunk == "[DONE]": break`).
DONE_SENTINEL = "[DONE]"


@dataclass
class LLMConfig:
    """Значения по умолчанию для секции [llm] (docs/config.md §4)."""

    url: str = "http://127.0.0.1:8080/v1/chat/completions"
    model: str = "local"
    temperature: float = 0.2
    max_tokens: int = 4000
    timeout: float = 300.0
    #: Параметры генерации, передаваемые на уровень запроса как есть
    #: (seed — для воспроизводимости, roadmap #4 §3.4; 0 отключает
    #: сидирование на стороне сервера).
    seed: Optional[int] = None
    stop: List[str] = field(default_factory=list)
    #: Вариант бэкенда для механизма think (stack.md §5.3):
    #: "llama_cpp" → параметр тела {"think": bool};
    #: "chatml"    → софт-промпт /no_think в последнем сообщении пользователя.
    backend: str = "llama_cpp"


class LlamaCppClient:
    """Стриминговый чат-клиент для сервера llama.cpp (OpenAI-совместимый API).

    Использование в качестве стыка LLM для агента::

        client = LlamaCppClient(LLMConfig())
        agent = Agent(name="decomposer", llm=lambda msgs: client.stream(msgs, use_think=True))

    или напрямую (``stream`` возвращает итератор; агент принимает и вызываемые
    объекты от сообщений — bound-метод + kwargs через partial тоже подходят).
    """

    def __init__(self, config: Optional[LLMConfig] = None,
                 session: Optional[Any] = None) -> None:
        self.config = config or LLMConfig()
        #: Инъектируемый транспорт: любой объект с методом
        #: .post(url, json, stream, timeout), возвращающий ответ с
        #: .raise_for_status(), .iter_lines() и .close().
        #: По умолчанию — свежий requests.Session.
        if session is None:
            import requests          # ленивый импорт: юнит-тесты без сети и зависимостей
            session = requests.Session()
        self._session = session

    # ------------------------------------------------------------------
    def build_payload(self, messages: List[Dict[str, str]], *,
                      use_think: bool = True,
                      temperature: Optional[float] = None,
                      max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Собрать тело запроса (чистая функция — тестируется офлайн).

        Механика think согласно stack.md §5.3:
        - llama.cpp: параметр запроса ``{"think": true|false}``;
        - запасной chatml: если think выключен, к последнему сообщению
          пользователя приклеивается ``/no_think`` (системный промпт
          остаётся байт-в-байт неизменным — P8).
        """
        msgs = [dict(m) for m in messages]
        cfg = self.config
        payload: Dict[str, Any] = {
            "model": cfg.model,
            "messages": msgs,
            "temperature": cfg.temperature if temperature is None else temperature,
            "max_tokens": cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": True,
        }
        if cfg.seed is not None:
            payload["seed"] = cfg.seed
        if cfg.stop:
            payload["stop"] = list(cfg.stop)

        if cfg.backend == "llama_cpp":
            # основной бэкенд: think управляется параметром тела запроса
            payload["think"] = bool(use_think)
        elif cfg.backend == "chatml":
            # fallback: софт-промпт /no_think в последнем user-сообщении
            if not use_think:
                for i in range(len(msgs) - 1, -1, -1):
                    if msgs[i].get("role") == "user":
                        msgs[i]["content"] = "/no_think\n" + msgs[i].get("content", "")
                        break
        else:
            # неизвестный вариант бэкенда — явная ошибка, а не тихий default
            raise ValueError(f"unknown backend flavour: {cfg.backend!r}")
        return payload

    # ------------------------------------------------------------------
    def stream(self, messages: List[Dict[str, str]], *,
               use_think: bool = True,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        """Выдавать сырые дельты content; завершить сентинелом ``[DONE]``.

        Разбор SSE-фрейминга (OpenAI-совместимого): строки вида
        ``data: {...}``` парсятся, извлекается ``delta.content``; пустые
        строки и комментарии игнорируются. Серверный ``data: [DONE]``
        нормализуется в тот же сентинел, который ожидает цикл агента.
        """
        payload = self.build_payload(messages, use_think=use_think,
                                     temperature=temperature,
                                     max_tokens=max_tokens)
        resp = self._session.post(
            self.config.url, json=payload, stream=True,
            timeout=self.config.timeout)
        try:
            resp.raise_for_status()
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue                     # пустые строки SSE — разделители событий
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if not raw.startswith("data:"):
                    continue                     # SSE-комментарии / keep-alive фреймы
                data = raw[len("data:"):].strip()
                if data == "[DONE]":
                    yield DONE_SENTINEL
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue                       # толерантность: битый фрейм пропускаем
                choices = obj.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content
            yield DONE_SENTINEL                  # чистый EOF без явного [DONE] от сервера
        finally:
            # всегда освобождаем соединение, даже при обрыве итерации сверху
            close = getattr(resp, "close", None)
            if callable(close):
                close()

    # ------------------------------------------------------------------
    def as_callable(self, *, use_think: bool = True
                    ) -> Callable[[List[Dict[str, str]]], Iterator[str]]:
        """Адаптер под контракт ``Agent(llm=...)``: callable(messages) -> чанки."""
        def _call(messages: List[Dict[str, str]]) -> Iterator[str]:
            return self.stream(messages, use_think=use_think)
        return _call

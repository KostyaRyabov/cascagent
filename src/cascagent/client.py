"""Транспортные клиенты LLM — стриминг через OpenAI-совместимый API (docs/stack.md §4–5).

Архитектура (рефакторинг 2026-10-14): различия бэкендов вынесены в иерархию
классов вместо размазанного по методам ``if backend == ...``:

* ``LLMClient``          — абстрактный интерфейс (ABC): контракт для вызывающей
  стороны (агент, диспетчер, CLI). Никто в проекте не должен зависеть от
  конкретного класса — только от этого интерфейса;
* ``LlamaCppLLMClient``  — llama.cpp server (:8080, ADR-001): think управляется
  параметром тела запроса ``{"think": bool}``;
* ``ChatmlLLMClient``    — TabbyAPI и прочие chatml-серверы: think переключается
  софт-промптом ``/no_think``, приклеиваемым к ПОСЛЕДНЕМУ сообщению
  пользователя (системный префикс остаётся байт-в-байт неизменным — P8);
* ``OpenAIChatClient`` / ``LlamaCppClient`` — алиасы эпохи «один класс +
  flavour» оставлены для обратной совместимости импортов.

Транспортный контракт, зафиксированный 2026-10-13 (roadmap.md §9.4,
plugins.md §4.2): стык LLM внутри ``Agent.run`` — это **итератор чанков**,
либо простой итерируемый объект, либо ``callable(messages) -> iterator[str]``.
``LLMClient.stream()`` — ровно такой вызываемый объект: выдаёт сырые текстовые
дельты (SSE ``delta.content``) и завершается сентинелом ``"[DONE]"``, который
потребляет цикл агента. Разделение на think/final — НЕ задача клиента: поток
структурирует встроенный ``ResponseParser`` (plugins.md §3.1).

Полный контракт (finish_reason, tool_calls) доступен через
``LLMClient.stream_events()`` — типизированные события см. ниже.

Соблюдаемые инварианты бэкенда (stack.md §4.3):
1. OpenAI-совместимый эндпоинт (ADR-001);
2. стабильный префикс SYSTEM передаёт вызывающая сторона (protocol §2) —
   клиент отправляет сообщения дословно, ничего не переписывая
   (единственное исключение — /no_think у ChatmlLLMClient, п.3);
3. переключатель think на уровне запроса (§5.3) — реализован в наследниках;
4. single-flight — один HTTP-запрос на вызов (AGENTS.md §9);
5. температура по умолчанию 0.2 (config.md §4 [llm]).

Транспорт инъектируемый (roadmap.md §10 Фаза 2 «transport инъектируем»):
конструктор принимает объект ``session`` с методом ``post(url, json=...,
stream=..., timeout=...)`` — в реальной работе ``requests.Session()``,
в тестах — фейк (MockLLM подаёт данные агенту напрямую, поэтому тесты
этого модуля не зависят от сети).

Граница «CPU-тонкий слой» (roadmap.md §1 п.1): модуль работает только с HTTP;
никакого SQLite и никаких эвристик парсинга ответа здесь нет.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

#: Сентинел, выдаваемый после последней дельты — соответствует проверке
#: в цикле агента (memory.py run(): `if chunk == "[DONE]": break`).
#: Используется как единственный способ «вежливо» сигнализировать верхнему
#: потребителю о завершении потока.
DONE_SENTINEL = "[DONE]"

#: Нормальные (не-ошибочные) причины завершения по OpenAI-спеке.
NORMAL_FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls"}


# ---------------------------------------------------------------------------
# Ошибки транспорта LLM (OpenAI-совместимый API)
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """Базовое исключение для всех ошибок клиента LLM."""


class LLMAborted(LLMError):
    """Генерация была прервана сервером: finish_reason не является нормальным.

    Нормальные причины завершения (``stop``, ``length``, ``content_filter``,
    ``tool_calls``) обрабатываются штатно; всё остальное (``"abort"``,
    ``"timeout"``, ``"unknown"`` и т.п.) приводит к этому исключению, если
    включён режим строгой проверки (см. ``LLMConfig.strict_finish``).
    """


class LLMToolCallNotSupported(LLMError):
    """Модель/бэкенд вернул tool_calls, но клиент не настроен на их обработку."""


# ---------------------------------------------------------------------------
# Типизация стриминговых событий
# ---------------------------------------------------------------------------
# Полный набор событий, которые может выдать stream_events():
#
#   str                 текстовая дельта (delta.content) — основной поток;
#   ReasoningDelta      дельта «мыслей» модели (delta.reasoning / reasoning_content):
#                       llama.cpp >= b5xxx и некоторые vLLM-форки отдают thinking
#                       ОТДЕЛЬНЫМИ полями, а не в content;
#   ToolCallDelta       частичный вызов инструмента (кусочки delta.tool_calls);
#   FinishEvent         РОВНО ОДНО в конце потока: finish_reason + агрегированные
#                       tool_calls + собранный текст.
#
# Чего здесь СОЗНАТЕЛЬНО нет:
# - RoleDelta (delta.role): в OpenAI-контракте роль ответа всегда "assistant"
#   и приходит одним служебным фреймом — бизнес-смысла для нас нет, терять
#   из-за неё content-дельты было бы ошибкой;
# - UsageEvent (токены prompt/completion): часть серверов шлёт usage в
#   финальном фрейме — пока игнорируется; при необходимости добавим полем
#   в FinishEvent (см. открытый вопрос в roadmap);
# - отдельных событий SSE-фрейминга (comment/keep-alive): это транспортный
#   шум, он не доходит до потребителя.
#
# Аварийные ситуации приходят НЕ событием, а исключением: LLMAborted /
# LLMError (генератор прерывается — сентинел и FinishEvent после них не
# выдаются). Исключение — мягкий режим strict_finish=False: обрыв там
# фиксируется как FinishEvent(finish_reason="abort").

StreamEvent = Union[str, "ReasoningDelta", "ToolCallDelta"]


@dataclass
class ReasoningDelta:
    """Дельта скрытого рассуждения модели (thinking/reasoning).

    Бэкенды отдают её по-разному: llama.cpp — поле ``reasoning`` в delta,
    некоторые серверы — ``reasoning_content``. Клиент нормализует оба в
    этот тип; вызывающая сторона решает, показывать ли мысли пользователю
    (структурирование think/final в терминале — задача ResponseParser,
    plugins.md §3.1, а не клиента).
    """

    text: str


@dataclass
class ToolCallDelta:
    """Частичный вызов инструмента из SSE ``delta.tool_calls``.

    OpenAI-совместимые серверы отдают аргументы инструментов потоково:
    сначала приходит заголовок (id/name), затем куски JSON-аргументов.
    Клиент не собирает их сам — он выдаёт ``ToolCallDelta`` по одному на
    каждый фрейм, чтобы вызывающий код мог агрегировать по своему вкусу.
    """

    index: int = 0
    id: Optional[str] = None
    name: Optional[str] = None
    arguments_delta: Optional[str] = None
    #: Сырой словарь из ответа сервера (для отладки и расширенных полей).
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FinishEvent:
    """Финальное событие потока: причина остановки и накопленные tool_calls.

    Не является исключением при нормальных finish_reason. Выдаётся
    исключительно внутри ``stream_events()``, когда поток дошёл до конца
    или получил ``data: [DONE]`` без явного выбора с finish_reason.
    """

    finish_reason: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    content_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    """Значения по умолчанию для секции [llm] (docs/config.md §4).

    Поле ``backend`` сохранено для обратной совместимости конфига, но
    больше НЕ управляет поведением: выбор реализации делается классом
    (фабрика ``create_client`` читает ``backend`` ровно один раз).
    """

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
    #: Устаревшее: вариант бэкенда ("llama_cpp" | "chatml"). Используется
    #: только фабрикой create_client() для выбора класса реализации.
    backend: str = "llama_cpp"
    #: Строгая проверка корректности завершения потока (finish_reason/[DONE]):
    #: если True, то обрыв соединения без finish_reason и [DONE], а также
    #: нестандартные причины ("abort", "timeout"…) приводят к LLMAborted.
    #: Если False — поток завершается мягко, причина доступна в FinishEvent.
    #: По умолчанию False: упрощённый контракт stream() исторически терпим
    #: к чистому EOF без [DONE] (см. тест clean_eof); строгость включается
    #: явно там, где нужен полный контроль (stream_events/complete).
    strict_finish: bool = False
    #: Разрешить обработку tool_calls из SSE delta.tool_calls. Если False,
    #: при появлении tool_calls будет выброшено LLMToolCallNotSupported.
    enable_tools: bool = True


# ---------------------------------------------------------------------------
# Интерфейс клиента
# ---------------------------------------------------------------------------

class LLMClient(ABC):
    """Абстрактный интерфейс клиента LLM (дорожная карта: transport инъектируем).

    Контракт для всего остального кода проекта: агент, диспетчер и CLI должны
    типизироваться этим классом, а не конкретными реализациями. Общие методы:

    * ``build_payload``  — собрать тело запроса из сообщений (чистая функция;
      различия бэкендов — единственная её «ветвящаяся» часть, реализуемая
      наследниками через хук ``apply_backend_flavour``);
    * ``stream``         — упрощённый чанк-поток под контракт ``Agent.run``
      (текст + сентинел [DONE]);
    * ``stream_events``  — полный контракт: типизированные события
      (str / ReasoningDelta / ToolCallDelta / FinishEvent);
    * ``complete``       — convenience: собрать весь поток в FinishEvent;
    * ``run_tool_loop``  — полный цикл function calling с исполнением
      инструментов;
    * ``as_callable``    — адаптер ``callable(messages) -> iterator[str]``.

    Транспорт (HTTP/SSE-разбор) общий для всех бэкендов — OpenAI-совместимость
    и есть причина, по которой наследники различаются ТОЛЬКО сборкой payload.
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

    # ---- обязательный хук наследника ---------------------------------
    @abstractmethod
    def apply_backend_flavour(self, payload: Dict[str, Any],
                              messages: List[Dict[str, Any]], *,
                              use_think: bool) -> None:
        """Применить специфику бэкенда к собранному payload (mutates).

        Реализации:
        - LlamaCppLLMClient: кладёт ``payload["think"] = bool(use_think)``;
        - ChatmlLLMClient: при use_think=False приклеивает ``/no_think``
          к последнему user-сообщению в payload["messages"].
        """

    # ---- сборка запроса ----------------------------------------------
    def build_payload(self, messages: List[Dict[str, Any]], *,
                      use_think: bool = True,
                      temperature: Optional[float] = None,
                      max_tokens: Optional[int] = None,
                      tools: Optional[List[Dict[str, Any]]] = None,
                      tool_choice: Optional[Any] = None) -> Dict[str, Any]:
        """Собрать тело запроса (чистая функция — тестируется офлайн).

        Общая часть одинакова для всех бэкендов; специфика — в хуке
        ``apply_backend_flavour``.

        Function calling (OpenAI-совместимый контракт):
        - ``tools`` — список описаний инструментов; добавляется в тело как есть;
        - ``tool_choice`` — режим выбора ("auto", "none", "required" или
          конкретная функция); прокидывается только вместе с tools.
        Сообщения передаются дословно (инвариант 2): вызывающая сторона сама
        формирует history, включая assistant-сообщения с ``tool_calls`` и
        сообщения с ``role="tool"`` (результаты выполнения инструментов).
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
            payload["seed"] = cfg.seed      # воспроизводимость генерации
        if cfg.stop:
            payload["stop"] = list(cfg.stop)
        if tools:
            payload["tools"] = list(tools)
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        # различия бэкендов — единая точка расширения
        self.apply_backend_flavour(payload, msgs, use_think=use_think)
        return payload

    # ---- полный контракт событий -------------------------------------
    def stream_events(self, messages: List[Dict[str, Any]], *,
                      use_think: bool = True,
                      temperature: Optional[float] = None,
                      max_tokens: Optional[int] = None,
                      tools: Optional[List[Dict[str, Any]]] = None,
                      tool_choice: Optional[Any] = None,
                      ) -> Iterator[Union[StreamEvent, FinishEvent]]:
        """Низкоуровневый стриминг событий SSE — полный контракт OpenAI API.

        Выдаёт по одному объекту на смысловое событие потока:
        - ``str``             — текстовая дельта (``delta.content``);
        - ``ReasoningDelta``  — дельта скрытых рассуждений (thinking);
        - ``ToolCallDelta``   — частичный вызов инструмента (``delta.tool_calls``);
        - ``FinishEvent``     — ровно ОДИН раз в конце: причина завершения
          (``finish_reason``) и агрегированные дополненные tool_calls.

        Обработка finish_reason:
        - нормальные причины: "stop", "length", "content_filter", "tool_calls";
        - при ``config.strict_finish=True`` любая НЕнормальная причина
          (например "abort"/"timeout" у нестандартных серверов) или обрыв
          потока без finish_reason вообще — выбрасывает ``LLMAborted``,
          что для генератора означает прекращение итерации исключением;
        - при ``strict_finish=False`` поток просто завершается, причина
          доступна в FinishEvent.

        Обработка tool_calls (стриминговый формат OpenAI):
        сервер шлёт их кусками — сначала фрейм с {index, id, function.name},
        затем несколько фреймов с {index, function.arguments: "<кусок JSON>"}.
        Мы НЕ парсим JSON аргументов здесь (он может прийти не целиком),
        вместо этого:
        - каждый кусок отдаём наружу как ``ToolCallDelta`` (для live-UI);
        - параллельно агрегируем по index; в ``FinishEvent.tool_calls``
          лежат собранные сообщения вида
          ``{"id":..., "type":"function", "function":{"name":..., "arguments":...}}``
          — ровно то, что надо положить в assistant-сообщение истории
          перед следующим запросом (цикл function calling).

        ``delta.role`` явно не транслируется: в OpenAI-контракте role приходит
        одним фреймом ("assistant") и несёт чисто информационный смысл —
        ответ модели ВСЕГДА assistant, а роль результатов инструментов
        ("tool") появляется только во ВХОДНЫХ сообщениях, которые формирует
        вызывающая сторона. Терять content-дельты из-за роли было бы ошибкой.

        Генератор всегда закрывает HTTP-соединение в finally (защита от
        утечки при раннем break/исключении сверху).
        """
        payload = self.build_payload(messages, use_think=use_think,
                                     temperature=temperature,
                                     max_tokens=max_tokens,
                                     tools=tools, tool_choice=tool_choice)
        resp = self._session.post(
            self.config.url, json=payload, stream=True,
            timeout=self.config.timeout)

        #: Агрегатор tool_calls по index: {index: {"id","name","args_parts"}}
        tc_acc: Dict[int, Dict[str, Any]] = {}
        finish_reason: Optional[str] = None
        got_done_frame = False               # сервер прислал data: [DONE]
        text_parts: List[str] = []

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
                if data == DONE_SENTINEL:
                    got_done_frame = True        # штатный конец потока от сервера
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue                       # толерантность: битый фрейм пропускаем

                # Некоторые бэкенды кладут ошибку прямо в тело потока.
                if obj.get("error"):
                    raise LLMError(f"server returned stream error: {obj['error']}")

                choices = obj.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}

                # --- текстовая дельта ------------------------------------
                content = delta.get("content")
                if content:
                    text_parts.append(content)
                    yield content

                # --- дельта рассуждений (thinking) ------------------------
                # normalize разных имён полей у разных бэкендов
                reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                if reasoning:
                    yield ReasoningDelta(reasoning)

                # --- tool_calls (стриминговые куски) ----------------------
                for tc in (delta.get("tool_calls") or []):
                    idx = int(tc.get("index", 0))
                    slot = tc_acc.setdefault(
                        idx, {"id": None, "name": None, "args_parts": []})
                    fn = tc.get("function") or {}
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    arg_delta = fn.get("arguments")
                    if arg_delta:
                        slot["args_parts"].append(arg_delta)
                    if not self.config.enable_tools:
                        raise LLMToolCallNotSupported(
                            "backend returned tool_calls but "
                            "LLMConfig.enable_tools is False")
                    yield ToolCallDelta(index=idx, id=tc.get("id"),
                                        name=fn.get("name"),
                                        arguments_delta=arg_delta, raw=dict(tc))

                # --- finish_reason (приходит в последнем осмысленном фрейме) ---
                fr = choice.get("finish_reason")
                if fr is not None:
                    finish_reason = fr
                    if self.config.strict_finish and \
                            fr not in NORMAL_FINISH_REASONS:
                        raise LLMAborted(
                            f"generation aborted by server: "
                            f"finish_reason={fr!r}")
                    # не прерываем цикл: ждём data: [DONE] как штатный финал

            # --- агрегация собранных tool_calls в формат сообщений -------
            tool_calls_msg = []
            for idx in sorted(tc_acc):
                slot = tc_acc[idx]
                tool_calls_msg.append({
                    "id": slot["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": slot["name"] or "",
                        "arguments": "".join(slot["args_parts"]),
                    },
                })

            # --- проверка корректности завершения потока -----------------
            # Такую же логику применяем и для strict_finish=False, чтобы
            # FinishEvent всегда содержал осмысленную причину.
            if not got_done_frame and finish_reason is None:
                # TCP-обры́в: сервер не успел ни сообщить причину,
                # ни прислать [DONE] — это неполный ответ.
                if self.config.strict_finish:
                    raise LLMAborted(
                        "stream truncated: connection closed without "
                        "finish_reason or [DONE]")
                else:
                    # мягкий режим: фиксируем обрыв как "abort", но не падаем
                    finish_reason = "abort"
            elif finish_reason is None and got_done_frame:
                # некоторые серверы шлют [DONE] без finish_reason-фрейма;
                # считаем это обычным "stop"
                finish_reason = "stop"

            yield FinishEvent(
                finish_reason=finish_reason or "stop",
                tool_calls=tool_calls_msg,
                content_text="".join(text_parts) if text_parts else None,
            )
        finally:
            # всегда освобождаем соединение, даже при обрыве итерации сверху
            close = getattr(resp, "close", None)
            if callable(close):
                close()

    # ---- упрощённый контракт Agent.run --------------------------------
    def stream(self, messages: List[Dict[str, Any]], *,
               use_think: bool = True,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        """Упрощённый чанк-стрим под контракт ``Agent.run`` (roadmap §9.4).

        Выдаёт ТОЛЬКО текстовые дельты и завершается сентинелом ``[DONE]`` —
        ровно то, что ожидает цикл агента (memory.py: `if chunk == "[DONE]"`).
        Служебные события (reasoning, tool_calls, finish_reason) здесь
        отбрасываются: полный контракт доступен через ``stream_events()``.

        Почему сентинел, а не «просто прекратить генератор»:
        - StopAsyncIteration/GeneratorExit — служебные сигналы Python, они
          не несут бизнес-смысла «генерация завершилась»; агентский цикл
          обязан отличить нормальный конец от plugin-stop (голосование
          runtime-плагинов) и от аварийного обрыва;
        - сентинел — часть зафиксированного контракта стыка 2026-10-13;
        - при strict_finish=True аварийное завершение и так приходит как
          исключение LLMAborted — сентинел после него не выдаётся.
        """
        for ev in self.stream_events(messages, use_think=use_think,
                                     temperature=temperature,
                                     max_tokens=max_tokens):
            if isinstance(ev, str):
                yield ev
        yield DONE_SENTINEL                  # штатный финал упрощённого потока

    # ---- convenience: весь ответ одним объектом -----------------------
    def complete(self, messages: List[Dict[str, Any]], *,
                 use_think: bool = True,
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None,
                 tools: Optional[List[Dict[str, Any]]] = None,
                 tool_choice: Optional[Any] = None,
                 ) -> FinishEvent:
        """Не стримящий convenience-вызов: собрать весь поток в FinishEvent.

        Полезен для tool-calling цикла и тестов: возвращает текст, причину
        завершения и полностью собранные tool_calls одним объектом.
        """
        final: Optional[FinishEvent] = None
        for ev in self.stream_events(messages, use_think=use_think,
                                     temperature=temperature,
                                     max_tokens=max_tokens,
                                     tools=tools, tool_choice=tool_choice):
            if isinstance(ev, FinishEvent):
                final = ev
        if final is None:                     # теоретически недостижимо, но защита
            raise LLMAborted("stream ended without FinishEvent")
        return final

    # ---- полный цикл function calling ----------------------------------
    def run_tool_loop(self, messages: List[Dict[str, Any]],
                      tool_map: Dict[str, Callable[..., Any]], *,
                      max_rounds: int = 8,
                      use_think: bool = True,
                      **kwargs: Any) -> str:
        """Полный цикл function calling: запрос → tool_calls → результаты → повтор.

        ``tool_map``: имя инструмента → python-функция (например, MCP-мост).
        Возвращает финальный текстовый ответ модели. Бросает LLMError,
        если модель просит инструмент, которого нет в tool_map, или если
        цикл не сошёлся за max_rounds раундов.

        Пример::

            text = client.run_tool_loop(
                [{"role": "user", "content": "Какая погода в Москве?"}],
                tool_map={"get_weather": get_weather_fn},
                tools=[...],
            )
        """
        msgs = [dict(m) for m in messages]
        tool_specs = kwargs.pop("tools", None)
        if tool_specs is None:
            # автоматически построить схемы из docstring'ов нельзя —
            # caller обязан дать точные схемы; пустой список запрещён:
            raise LLMError("run_tool_loop requires explicit 'tools=[...]' specs")
        rounds = 0
        while True:
            rounds += 1
            if rounds > max_rounds:
                raise LLMError(f"tool loop exceeded {max_rounds} rounds")
            fin = self.complete(msgs, tools=tool_specs,
                                use_think=use_think, **kwargs)
            if not fin.tool_calls:
                return fin.content_text or ""
            # дописываем assistant-сообщение с запрошенными инструментами
            asst: Dict[str, Any] = {"role": "assistant",
                                    "tool_calls": fin.tool_calls}
            if fin.content_text:
                asst["content"] = fin.content_text
            msgs.append(asst)
            # выполняем каждый запрошенный инструмент и кладём результат
            # в сообщение role="tool" (современное имя; "function" — легаси-алиас)
            for call in fin.tool_calls:
                name = call["function"]["name"]
                fn = tool_map.get(name)
                if fn is None:
                    raise LLMError(f"model requested unknown tool: {name!r}")
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                    result = fn(**args)
                except Exception as exc:      # ошибку тоже отдаём модели
                    result = f"ERROR: {exc}"
                msgs.append({"role": "tool",
                             "tool_call_id": call["id"],
                             "content": (result if isinstance(result, str)
                                         else json.dumps(result,
                                                         ensure_ascii=False))})

    # ---- адаптер под агента ---------------------------------------------
    def as_callable(self, *, use_think: bool = True
                    ) -> Callable[[List[Dict[str, Any]]], Iterator[str]]:
        """Адаптер под контракт ``Agent(llm=...)``: callable(messages) -> чанки."""
        def _call(messages: List[Dict[str, Any]]) -> Iterator[str]:
            return self.stream(messages, use_think=use_think)
        return _call


# ---------------------------------------------------------------------------
# Реализации под конкретные бэкенды
# ---------------------------------------------------------------------------

class LlamaCppLLMClient(LLMClient):
    """Клиент llama.cpp server (:8080, ADR-001 — дефолтный бэкенд проекта).

    Специфика: think переключается параметром тела запроса ``{"think": bool}``
    (stack.md §5.3). Рассуждение при think=true приходит отдельным полем
    ``delta.reasoning`` — stream_events транслирует его как ReasoningDelta.
    """

    def apply_backend_flavour(self, payload: Dict[str, Any],
                              messages: List[Dict[str, Any]], *,
                              use_think: bool) -> None:
        # основной бэкенд: think управляется параметром тела запроса
        payload["think"] = bool(use_think)


class ChatmlLLMClient(LLMClient):
    """Клиент TabbyAPI / прочих chatml-серверов (fallback-бэкенд, stack.md §5.3).

    Специфика: сервер не понимает параметр ``think`` — выключение рассуждений
    достигается софт-промптом ``/no_think``, приклеиваемым к ПОСЛЕДНЕМУ
    сообщению пользователя. Системный префикс при этом остаётся
    байт-в-байт неизменным (P8 — стабильность KV-кэша).
    """

    def apply_backend_flavour(self, payload: Dict[str, Any],
                              messages: List[Dict[str, Any]], *,
                              use_think: bool) -> None:
        # fallback: софт-промпт /no_think в последнем user-сообщении
        if not use_think:
            msgs = payload["messages"]
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user":
                    msgs[i]["content"] = "/no_think\n" + msgs[i].get("content", "")
                    break


#: Таблица фабрики: значение конфига [llm].backend → класс клиента.
BACKEND_REGISTRY: Dict[str, type] = {
    "llama_cpp": LlamaCppLLMClient,
    "chatml": ChatmlLLMClient,
}


def create_client(config: Optional[LLMConfig] = None,
                  session: Optional[Any] = None) -> LLMClient:
    """Фабрика: выбрать реализацию ``LLMClient`` по ``config.backend``.

    Единственное место, где строковое имя бэкенда превращается в класс.
    Неизвестное имя — явная ошибка, а не тихий default (как было в эпоху
    «один класс + if»).
    """
    cfg = config or LLMConfig()
    cls = BACKEND_REGISTRY.get(cfg.backend)
    if cls is None:
        raise ValueError(
            f"unknown backend flavour: {cfg.backend!r}; "
            f"known: {sorted(BACKEND_REGISTRY)}")
    return cls(cfg, session=session)


# Алиасы для обратной совместимости имён эпохи «один класс + flavour».
# Новому коду следует использовать LLMClient / LlamaCppLLMClient /
# ChatmlLLMClient / create_client.
OpenAIChatClient = LlamaCppLLMClient
LlamaCppClient = LlamaCppLLMClient

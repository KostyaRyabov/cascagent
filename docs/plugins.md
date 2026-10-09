# cascagent — Плагинная архитектура агентов

> **Статус:** ⬜ план (архитектурный слой v3 поверх работающего ядра v2).
> Базовые классы из §6 реализуются на **Этапе 5** вместе с Research/Reflect
> агентами; до этого Decomposer работает монолитным `DecomposerSession`.
> Смежные документы: `docs/architecture.md` (обзор модулей),
> `docs/protocol.md` (протокол декомпозиции), `docs/process.md` (конвейер
> обогащения контекста), `docs/prompts.md` (тексты промптов),
> `docs/config.md` (формат конфигурации агентов).

---

## Содержание

1. Миссия и философия
2. Основные принципы
3. Архитектура агента
4. Четыре секции плагинов
5. SessionMemory — общая память
6. Базовые классы
7. Каталог встроенных плагинов
8. Конфигурации агентов
9. Жизненный цикл
10. Структура проекта
11. Примеры использования
12. Расширение системы

---

## 1. Миссия и философия

**cascagent** — мультиагентная система рекурсивной декомпозиции сложных
задач на атомарные подзадачи, работающая на слабом железе (4GB VRAM).

### Ключевая идея

> **LLM занимается ТОЛЬКО семантикой и рассуждениями. Всё остальное —
> маршрутизация, поиск, валидация, управление памятью, исправление
> опечаток — делает CPU через детерминированные алгоритмы.**

### Почему это работает

1. **Малые модели (4B–8B) плохо работают с форматом.** JSON-схемы,
   обязательные поля, структура — всё это ломается.
2. **LLM не нужна память системы.** Kanban, список инструментов, историю
   других агентов должна хранить база данных, а не контекст модели.
3. **Каждый токен контекста дорог.** На 4GB VRAM нет места для схем 1000
   MCP-инструментов.
4. **CPU делает простые операции быстрее.** Левенштейн, BK-tree, cosine
   similarity — микросекунды вместо секунд LLM.

Плагинная архитектура распространяет тот же принцип на **код системы**:
агент остаётся тонким «клеем», а вся переиспользуемая логика (обогащение,
парсинг, фильтрация, статистика) выносится в независимые, тестируемые
плагины четырёх секций.

### Именование

**cascagent** = **casc**ade + **agent** — каскадная декомпозиция задач,
где результаты «стекают» снизу вверх, а декомпозиция распространяется
сверху вниз.

### Лицензия

**Apache 2.0** — совместимость с AI-стеком (Qwen3, HuggingFace) и
патентная защита.

---

## 2. Основные принципы

### 2.1 Изоляция агентов

Каждый агент работает в **полностью изолированной сессии**:

- каждый вызов — отдельный HTTP-запрос, не продолжение чата;
- нет общей памяти между агентами (общее — только Kanban / semantic memory
  через CPU-слой);
- параллельная работа без конфликтов;
- предсказуемое потребление VRAM.

> Ограничение движка: llama.cpp server (действующий бэкенд, ADR-001 пока
> Proposed) не имеет slot API. Изоляция достигается тем, что каждый вызов
> отправляет полный набор сообщений (system + user) без истории прошлых
> вызовов. Slot-изоляция и save/restore KV-кэша возможны только при переходе
> на TabbyAPI — код плагинов не должен предполагать наличие слотов.

### 2.2 Минимизация контекста

Передаём в LLM только то, что невозможно вычислить на CPU:

| Было (LLM) | Стало (CPU) |
|-----------|-------------|
| Выбор инструмента из 1000 MCP | BK-tree fuzzy-поиск |
| Исправление опечаток | Расстояние Левенштейна |
| Поиск похожих задач | Эмбеддинги + cosine similarity |
| Валидация JSON | line/YAML-парсер с эвристиками |
| Состояние задач | SQLite Kanban |

### 2.3 Минимальный системный промпт

Модель знает как можно меньше о системе. Стандартный промпт Decomposer
(полная версия — docs/prompts.md):

```
Разбей задачу на подзадачи следующего уровня детализации.

КАЖДАЯ ПОДЗАДАЧА:
Название задачи
    Подробное описание что нужно сделать

ПРАВИЛА:
- Подзадачи должны полностью покрывать исходную задачу
- Каждая подзадача — конкретное действие
- Не повторяй исходную задачу
- Если задача не делится — напиши только: <atom>
```

~50 токенов вместо 500–800 в раздутых промптах. Именно поэтому
PromptCompiler компилирует этот префикс один раз, а все input-плагины
добавляют динамический контекст строго в user-сообщение — иначе рвётся
префиксный кэш сервера (см. §12.2 п.2 и docs/performance.md).

### 2.4 Честный парсинг

- всегда разделять THINK и FINAL RESPONSE, логировать оба в debug.log;
- НЕ обрезать список подзадач (лимиты = потеря информации);
- атомарность определяется только по точному совпадению `<atom>`;
- без эвристик («если содержит hello world — атом»).

В плагинной терминологии: ThinkBlockExtractor (trigger) пишет think-блок
в local-память, HistoryLogger (output) сохраняет ОБА блока — ничего не
теряется на промежуточных секциях.

### 2.5 Ленивая декомпозиция

Дерево строится по мере выполнения, а не заранее (ADR-004):

- контекст растёт от результатов предыдущих задач;
- ранние решения могут быть отменены Reflect-агентом;
- экономия токенов (не декомпозируем отменённые ветки).

### 2.6 Принцип «плагин ≠ бизнес-логика агента»

Плагин отвечает на вопрос «как обработать данные на этой фазе цикла»,
а не «что делать агенту». Порядок плагинов внутри секции значим и
фиксируется конфигурацией; плагины не вызывают друг друга напрямую —
только через SessionMemory (§5).

---

## 3. Архитектура агента

### 3.1 Агент как композиция

**Агент = Промпт + Плагины + Инструменты**

Агент сам по себе — только «клей». Вся бизнес-логика живёт в плагинах.
Это даёт:

- переиспользование плагинов между агентами (один ContextEnricher для
  Decomposer, Executor и Researcher);
- тестируемость каждого компонента изолированно (unit-тест плагина не
  требует LLM);
- возможность создавать новых агентов БЕЗ написания кода — только
  конфигурацией (TOML, см. §8 и docs/config.md).

### 3.2 Структура агента

```
+-----------------------------------------------------+
|                     AGENT                           |
|                                                     |
|  system_prompt: str                                 |
|  tools: list[MCPTool]                               |
|                                                     |
|  [INIT plugins]   [INPUT plugins]   [TRIGGER plugins]|
|                                                     |
|  [OUTPUT plugins]      SessionMemory                |
|                        +- global (persistent)       |
|                        +- local  (per-run)          |
+-----------------------------------------------------+
```

### 3.3 Состояния агента

1. **Unbound** — только создан, плагины не инициализированы;
2. **Bound** — прошёл через init-плагины, готов к работе;
3. **Running** — выполняет run(task), использует память.

```python
agent = Agent(...)         # Unbound
agent.bind()               # Bound (init-плагины отработали)
result = agent.run(task)   # Running
```

Повторный bind() запрещён (бросает RuntimeError): сессия пересоздаётся
только новым экземпляром агента — это гарантирует воспроизводимость (§2.1).

---

## 4. Четыре секции плагинов

| Секция | Когда | Частота | Назначение |
|--------|-------|---------|-----------|
| **Init** | При bind() | 1 раз за сессию | Подготовка: индексы, прекомпиляция, health check |
| **Input** | Перед каждой генерацией | N раз | Обогащение, фильтрация ввода |
| **Trigger** | На каждом токене стрима | На каждый токен | Фильтры на лету, ранний стоп, извлечение think |
| **Output** | После завершения генерации | N раз | Парсинг, валидация, retry, логирование |

### 4.1 Init-плагины

Выполняются один раз при bind(). Используются для тяжёлой подготовки.

Типичные задачи:

- загрузка индексов (BK-tree, RAG, FAISS);
- прекомпиляция системного промпта с few-shot;
- проверка доступности LLM-эндпоинта (health check — GET /v1/models,
  НЕ генерация: dummy-warmup-запросы сжигают время и могут сбивать
  префиксный кэш — см. docs/performance.md);
- инициализация подписок на события памяти.

```python
class IndexLoader(InitPlugin):
    """Загружает BK-tree инструментов."""

    def __init__(self, tool_names: list[str]):
        self.tool_names = tool_names

    def on_init(self, session, memory):
        tree = BKTree()
        for name in self.tool_names:
            tree.add(name)
        session.indices["tools"] = tree
        memory.set_global("tools_loaded", True)
        return session
```

### 4.2 Input-плагины

Выполняются перед каждым вызовом LLM. Модифицируют messages перед отправкой.

Типичные задачи:

- обогащение контекста (RAG + semantic + siblings) — конвейер подробно
  описан в docs/process.md §2;
- фильтрация PII во входе;
- обрезка длинной истории (для multi-turn агентов);
- подстановка переменных;
- rate limiting.

```python
class ContextEnricher(InputPlugin):
    """Собирает контекст из всех источников."""

    def on_input(self, messages, context, session, memory):
        task = context["task"]

        # Проверяем кэш в local памяти (для multi-turn)
        cache_key = f"enrichment_{task.id}"
        cached = memory.get_local(cache_key)

        if not cached:
            docs = self.rag.search(task.brief, top_k=3)
            memories = self.semantic.recall(task.brief, top_k=3)
            siblings = self.kanban.get_completed_siblings(task.id)
            cached = self._format(docs, memories, siblings)
            memory.set_local(cache_key, cached)

        # Добавляем в последний user message — НЕ трогаем system!
        original = messages[-1]["content"]
        messages[-1]["content"] = f"{original}\n\nКонтекст:\n{cached}"
        return messages
```

### 4.3 Trigger-плагины

Работают на каждом токене во время стриминговой генерации. Самые мощные
и опасные: исполняются в горячем цикле, стоимость одного плагина
умножается на количество токенов (бюджет — §9.2, запрет I/O — §12.2 п.3).

Типичные задачи:

- ранний стоп по маркерам (например, конец финального ответа);
- фильтрация PII на лету;
- извлечение think-блоков;
- принудительное форматирование;
- логирование в реальном времени.

Эталонный плагин секции — извлечение think-блока на лету:

```python
class ThinkBlockExtractor(TriggerPlugin):
    """Разделяет <think>...</think> и финальный ответ во время стрима."""

    def __init__(self):
        self.in_think = False
        self.think_buffer = []
        self.final_buffer = []

    def on_token(self, token, accumulated, session, memory):
        if "<think>" in token:
            self.in_think = True
        elif "</think>" in token:
            self.in_think = False

        if self.in_think:
            self.think_buffer.append(token)
        else:
            self.final_buffer.append(token)

        return {"action": "continue"}

    def on_stream_end(self, full_response, session, memory):
        # Сохраняем think в LOCAL для output-плагинов и HistoryLogger
        memory.set_local("think_content", "".join(self.think_buffer))
        memory.set_local("think_length", len("".join(self.think_buffer)))

        think_buf, final_buf = self.think_buffer, self.final_buffer
        self.think_buffer, self.final_buffer = [], []

        return "".join(final_buf).strip()
```

**API результата trigger-плагина:**

```python
{
    "action": "continue" | "stop" | "modify",
    "modified_token": str,  # опционально, только для "modify"
    "reason": str,          # опционально, для логирования
}
```

Правила секции:

- `on_token` обязан отрабатывать за микросекунды (никакого I/O!);
- состояние плагина сбрасывается в `on_stream_end` — один экземпляр
  переиспользуется между run();
- порядок trigger-плагинов = конвейер: следующий получает уже модифицированный
  токен;
- первый вернувший `"stop"` завершает генерацию (остальные не вызываются).

### 4.4 Output-плагины

Выполняются после завершения генерации. Постобработка финального ответа.

Типичные задачи:

- парсинг ответа в структуру (список подзадач по протоколу v2,
  docs/protocol.md §3);
- удаление дубликатов (DuplicateDetector, порог 0.75);
- проверка качества (например, вырожденный повтор родителя = атом);
- логирование в историю (JSONL + debug.log — ОБА блока, см. §2.4);
- retry при ошибках парсинга.

```python
class ResponseParser(OutputPlugin):
    """Парсит текстовый ответ в список задач."""

    def __init__(self, parser_fn):
        self.parse = parser_fn

    def on_output(self, response, context, session, memory):
        try:
            parsed = self.parse(response)   # parse_decomposition из parser.py
            memory.set_local("parsed", parsed)
            return {
                "response": response,
                "parsed": parsed,
                "action": "accept",
            }
        except ParseError as e:
            memory.set_local("parse_error", str(e))
            return {
                "response": response,
                "action": "retry",  # попробуем ещё раз
            }
```

**API результата output-плагина:**

```python
{
    "response": str,       # может быть модифицирован
    "parsed": Any,         # опционально
    "metadata": dict,      # произвольные метаданные
    "action": "accept" | "retry" | "reject",
}
```

Важно: `retry` перезапускает ВЕСЬ цикл run() (input → генерация → output),
а не только генерацию; число ретраев ограничено конфигом (`max_retries`,
по умолчанию 2), иначе — `AgentRejectedError`.

---

## 5. SessionMemory — общая память

### 5.1 Концепция

SessionMemory — двухуровневое хранилище данных, доступное всем плагинам
ОДНОГО агента. Это единственный легальный канал коммуникации между
плагинами (§2.6). Между агентами память НЕ разделяется (§2.1).

```
+-------------------------------------+
|          SessionMemory              |
+-------------------------------------+
| GLOBAL (живёт всю сессию)           |
|  - conversation_history: list       |
|  - stats: dict                      |
|  - think_stats: dict                |
|  - rate_limit_calls: list           |
|  - [любые ключи от плагинов]        |
|                                     |
| LOCAL (сбрасывается каждый run)     |
|  - think_content: str               |
|  - parse_errors: list               |
|  - enrichment_{id}: str             |
|  - [любые ключи от плагинов]        |
+-------------------------------------+
```

### 5.2 Два скоупа

| Скоуп | Время жизни | Сбрасывается | Примеры использования |
|-------|------------|--------------|----------------------|
| **global** | Всю сессию агента | При пересоздании | История диалога, статистика, конфиги |
| **local** | Один вызов run() | В начале каждого run() | Think-блоки, промежуточные результаты, кэш в рамках вызова |

### 5.3 API памяти

```python
class SessionMemory:
    # === GLOBAL ===
    def set_global(self, key: str, value: Any) -> None: ...
    def get_global(self, key: str, default: Any = None) -> Any: ...
    def update_global(self, key: str, updater: callable, default: Any = None) -> Any: ...

    # === LOCAL ===
    def set_local(self, key: str, value: Any) -> None: ...
    def get_local(self, key: str, default: Any = None) -> Any: ...
    def update_local(self, key: str, updater: callable, default: Any = None) -> Any: ...

    # === УПРАВЛЕНИЕ ===
    def reset_local(self) -> None: ...   # вызывается автоматически в начале run()
    def reset_all(self) -> None: ...     # при пересоздании сессии

    # === ПОДПИСКИ (event-driven) ===
    def subscribe(self, key: str, callback: callable) -> None: ...

    # === УТИЛИТЫ ===
    def snapshot(self) -> dict: ...      # снимок всей памяти
```

### 5.4 Event-driven подписки

Плагины могут подписываться на изменения ключей (регистрация — в init):

```python
# В init-плагине
memory.subscribe("response_duration", self._on_duration)

# Когда где-то вызывается
memory.set_local("response_duration", {"duration_ms": 1500})

# Автоматически срабатывает
def _on_duration(self, key, value, scope):
    # обновить статистику
    pass
```

Подписчики вызываются синхронно; исключение в подписчике логируется и НЕ
прерывает set_*(); подписки живут до конца сессии (отписка не предусмотрена
— сессия короткоживущая).

### 5.5 Паттерны использования

**Producer-Consumer (Trigger → Output):**

```python
# Trigger плагин пишет в local
memory.set_local("think_content", think_block)

# Output плагин читает
think = memory.get_local("think_content", "")
```

**Счётчики (атомарное обновление):**

```python
memory.update_global("total_calls", lambda x: x + 1, default=0)
```

**Кэш в рамках run():**

```python
cached = memory.get_local(f"enrichment_{task.id}")
if not cached:
    cached = expensive_computation()
    memory.set_local(f"enrichment_{task.id}", cached)
```

**Флаги координации:**

```python
# Плагин A
memory.set_local("needs_research", True)
memory.set_local("research_query", "как работает X")

# Плагин B
if memory.get_local("needs_research"):
    query = memory.get_local("research_query")
    # запустить исследование
```

### 5.6 Соглашения об именовании ключей

| Префикс ключа | Скоуп | Пример | Кто пишет |
|---------------|-------|--------|-----------|
| `think_*` | local | `think_content`, `think_length` | Trigger |
| `enrichment_*` | local | `enrichment_0.2.1` | Input |
| `parsed`, `parse_error` | local | результат парсинга | Output |
| `stats`, `*_stats` | global | `think_stats`, `token_stats` | StatisticsCollector |
| `total_calls`, `rate_limit_calls` | global | счётчики | Init/Output |
| `needs_*`, флаги | local | `needs_research` | любой |

Ключи без префикса — локальные переменные одного плагина, другим секциям
не гарантируются.

### 5.7 Расширение: stats и breakpoints (решение аудита #3)

Зафиксировано в `docs/roadmap.md` §3.3: для восстановления после сбоев
SessionMemory получает два дополнительных **персистентных** отдела вдобавок
к global/local:

```python
class SessionMemory:
    _global: dict        # живёт всю сессию агента (как раньше)
    _local: dict         # сбрасывается каждый run() (как раньше)
    _stats: dict         # persist: счётчики вызовов, латентность, токены
    _breakpoints: list   # persist: именованные дампы state для resume
```

Дополнительный API (основное поведение §5.3 не меняется):

```python
def set_stats(self, key, value): ...        # как set_global, но попадает
def get_stats(self, key, default=None): ... #   в to_dump() всегда
def add_breakpoint(self, name, state): ...  # append {name, ts, state}
def last_breakpoint(self): ...              # или None
def to_dump(self) -> dict: ...              # {global, stats, breakpoints}
@classmethod
def from_dump(cls, data) -> "SessionMemory": ...
```

Правила:

- `reset_local()` **не трогает** stats/breakpoints; `reset_all()` трогает всё.
- Дамп на диск делает `CheckpointSaver` (Output) в
  `sessions/{agent_name}/{session_id}.json` при ключевых событиях
  (успех run, FAILED задачи, конец поддерева); загрузка — `CheckpointLoader`
  (Init). Механика `cascagent resume` — `docs/roadmap.md` §3.3.
- Подписки (§5.4) работают и на stats-ключи.
- Логику MemoryEntry/актуальности semantic memory (#8) см. roadmap §4.4 —
  это уровень `semantic.py`, а не SessionMemory.

---

## 6. Базовые классы

Полный код базовых классов для расширения. Реализуются в модулях
`src/cascagent/plugins/base.py` (классы плагинов и Agent) и
`src/cascagent/memory.py` (SessionMemory).

### 6.1 SessionMemory

```python
from typing import Any, Callable


class SessionMemory:
    """Двухуровневая память для плагинов."""

    def __init__(self):
        self._global: dict[str, Any] = {}
        self._local: dict[str, Any] = {}
        self._subscribers: dict[str, list[Callable]] = {}

    # === GLOBAL ===

    def set_global(self, key: str, value: Any):
        self._global[key] = value
        self._notify(key, value, "global")

    def get_global(self, key: str, default: Any = None) -> Any:
        return self._global.get(key, default)

    def update_global(self, key: str, updater: Callable, default: Any = None) -> Any:
        current = self._global.get(key, default)
        new_value = updater(current)
        self._global[key] = new_value
        self._notify(key, new_value, "global")
        return new_value

    # === LOCAL ===

    def set_local(self, key: str, value: Any):
        self._local[key] = value
        self._notify(key, value, "local")

    def get_local(self, key: str, default: Any = None) -> Any:
        return self._local.get(key, default)

    def update_local(self, key: str, updater: Callable, default: Any = None) -> Any:
        current = self._local.get(key, default)
        new_value = updater(current)
        self._local[key] = new_value
        self._notify(key, new_value, "local")
        return new_value

    # === УПРАВЛЕНИЕ ===

    def reset_local(self):
        self._local.clear()

    def reset_all(self):
        self._global.clear()
        self._local.clear()

    # === ПОДПИСКИ ===

    def subscribe(self, key: str, callback: Callable):
        if key not in self._subscribers:
            self._subscribers[key] = []
        self._subscribers[key].append(callback)

    def _notify(self, key: str, value: Any, scope: str):
        for cb in self._subscribers.get(key, []):
            try:
                cb(key, value, scope)
            except Exception as e:
                print(f"[memory] subscriber error for {key}: {e}")

    def snapshot(self) -> dict:
        return {
            "global": dict(self._global),
            "local": dict(self._local),
        }
```

### 6.2 AgentSession

```python
from dataclasses import dataclass, field


@dataclass
class AgentSession:
    """Состояние сессии агента."""
    agent_name: str
    system_prompt: str
    tools: list = field(default_factory=list)

    # Заполняется init-плагинами
    cache_key: str | None = None
    indices: dict = field(default_factory=dict)
    precompiled_prompt: str | None = None
    metadata: dict = field(default_factory=dict)
```

### 6.3 Базовые классы плагинов

```python
from abc import ABC, abstractmethod


class InitPlugin(ABC):
    name: str = "base_init"

    @abstractmethod
    def on_init(self, session: AgentSession, memory: SessionMemory) -> AgentSession:
        pass


class InputPlugin(ABC):
    name: str = "base_input"

    @abstractmethod
    def on_input(self, messages: list, context: dict,
                 session: AgentSession, memory: SessionMemory) -> list:
        pass


class TriggerPlugin(ABC):
    name: str = "base_trigger"

    @abstractmethod
    def on_token(self, token: str, accumulated: str,
                 session: AgentSession, memory: SessionMemory) -> dict:
        pass

    def should_stop(self, accumulated: str,
                    session: AgentSession, memory: SessionMemory) -> bool:
        return False

    def on_stream_end(self, full_response: str,
                      session: AgentSession, memory: SessionMemory) -> str:
        return full_response


class OutputPlugin(ABC):
    name: str = "base_output"

    @abstractmethod
    def on_output(self, response: str, context: dict,
                  session: AgentSession, memory: SessionMemory) -> dict:
        pass
```

### 6.4 Agent (оркестратор)

```python
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentResult:
    response: str
    parsed: Any = None
    metadata: dict = field(default_factory=dict)


class AgentRejectedError(Exception):
    """Output-плагины отклонили ответ (ретраи исчерпаны)."""


class Agent:
    """Агент как композиция промпта + плагинов + инструментов."""

    def __init__(
        self,
        name: str,
        system_prompt: str,
        llm_client,
        init_plugins=None,
        input_plugins=None,
        trigger_plugins=None,
        output_plugins=None,
        tools=None,
        max_retries: int = 2,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.llm = llm_client
        self.tools = tools or []
        self.max_retries = max_retries

        self.init_plugins = init_plugins or []
        self.input_plugins = input_plugins or []
        self.trigger_plugins = trigger_plugins or []
        self.output_plugins = output_plugins or []

        self.session = None
        self.memory = None
        self._retries = 0

    def bind(self) -> "Agent":
        """Инициализация сессии. Вызывается ровно один раз."""
        if self.session is not None:
            raise RuntimeError("Agent already bound")

        self.memory = SessionMemory()
        self.session = AgentSession(
            agent_name=self.name,
            system_prompt=self.system_prompt,
            tools=self.tools,
        )

        for plugin in self.init_plugins:
            self.session = plugin.on_init(self.session, self.memory)

        return self

    def run(self, task, context: dict | None = None) -> AgentResult:
        """Полный цикл: input -> LLM (с triggers) -> output."""
        if not self.session:
            raise RuntimeError("Agent must be bound before run()")

        # СБРОС local памяти
        self.memory.reset_local()

        context = dict(context or {})
        context["task"] = task
        context["call_start_time"] = time.time()

        # Формируем messages (system — статический префикс!)
        messages = [
            {"role": "system",
             "content": self.session.precompiled_prompt or self.system_prompt},
            {"role": "user", "content": f"Задача: {task.brief}"},
        ]

        # INPUT плагины
        for plugin in self.input_plugins:
            messages = plugin.on_input(messages, context, self.session, self.memory)

        context["full_prompt"] = "\n\n".join(m["content"] for m in messages)

        # ГЕНЕРАЦИЯ с TRIGGER плагинами
        raw_response = self._generate_with_triggers(messages, context)

        context["duration_ms"] = int((time.time() - context["call_start_time"]) * 1000)
        context["raw_response"] = raw_response
        self.memory.set_local("response_duration", {
            "duration_ms": context["duration_ms"],
            "task_id": task.id,
        })

        # OUTPUT плагины
        response = raw_response
        parsed = None
        metadata = {}

        for plugin in self.output_plugins:
            result = plugin.on_output(response, context, self.session, self.memory)
            response = result.get("response", response)
            parsed = result.get("parsed", parsed)
            metadata.update(result.get("metadata", {}))

            action = result.get("action", "accept")
            if action == "retry":
                if self._retries >= self.max_retries:
                    raise AgentRejectedError(result)
                self._retries += 1
                return self.run(task, context)   # повтор всего цикла
            elif action == "reject":
                raise AgentRejectedError(result)

        self._retries = 0
        return AgentResult(response=response, parsed=parsed, metadata=metadata)

    def _generate_with_triggers(self, messages, context) -> str:
        """Стриминговая генерация с применением trigger-плагинов."""
        accumulated = ""

        for token in self.llm.stream(messages):
            final_token = token
            should_stop = False

            for plugin in self.trigger_plugins:
                if plugin.should_stop(accumulated + final_token,
                                      self.session, self.memory):
                    should_stop = True
                    break

                result = plugin.on_token(final_token, accumulated,
                                         self.session, self.memory)

                if result["action"] == "stop":
                    should_stop = True
                    break
                elif result["action"] == "modify":
                    final_token = result.get("modified_token", final_token)

            accumulated += final_token
            if should_stop:
                break

        # Финализация
        for plugin in self.trigger_plugins:
            accumulated = plugin.on_stream_end(accumulated,
                                               self.session, self.memory)

        return accumulated
```

---

## 7. Каталог встроенных плагинов

### 7.1 Init-плагины

| Плагин | Назначение |
|--------|-----------|
| `IndexLoader` | Загружает BK-tree инструментов в `session.indices` |
| `PromptCompiler` | Прекомпилирует system prompt + few-shot в статический префикс |
| `HealthChecker` | Проверяет доступность LLM-эндпоинта (GET /v1/models), без генерации |
| `StatisticsCollector` | Подписывается на события памяти для сбора статистики |
| `RateLimiter` | Инициализирует счётчики rate limiting в global памяти |

### 7.2 Input-плагины

| Плагин | Назначение |
|--------|-----------|
| `ContextEnricher` | RAG + semantic memory + siblings (docs/process.md §2) |
| `SafetyInputFilter` | Маскирует PII (email, карты, API keys) во входе |
| `HistoryTrimmer` | Обрезает длинную историю для multi-turn агентов |
| `VariableSubstitution` | Подставляет `{project_name}`, `{date}` |
| `RateLimitCheck` | Проверяет и применяет rate limiting |
| `ConversationHistory` | Добавляет историю диалога из global памяти |

### 7.3 Trigger-плагины

| Плагин | Назначение |
|--------|-----------|
| `ThinkBlockExtractor` | Разделяет think/final на лету, пишет think в local память |
| `EarlyStopper` | Останавливает генерацию по стоп-маркерам (например, маркер конца ответа чата) |
| `PIIFilter` | Маскирует чувствительные данные на лету |
| `TokenLogger` | Логирует прогресс генерации в debug.log (с троттлингом записи) |
| `LengthGuard` | Предупреждает о приближении к limit контекстного окна |

### 7.4 Output-плагины

| Плагин | Назначение |
|--------|-----------|
| `ResponseParser` | Парсит decompose-формат (`Название` + `    Описание`, `<atom>`) |
| `DuplicateFilter` | Удаляет дубликаты подзадач (DuplicateDetector, порог 0.75) |
| `QualityValidator` | Проверки: пустой ответ, вырожденный повтор родителя, мусор |
| `HistoryLogger` | Пишет DecompositionCall в JSONL + THINK/FINAL в debug.log |
| `SemanticStore` | Сохраняет результат в semantic memory (remember) |
| `MetricsCollector` | Собирает токены/время/стоимость в global статистику |

---

## 8. Конфигурации агентов

Агенты описываются в TOML (см. `docs/config.md`): список секций и параметров
плагинов. Порядок плагинов в списке = порядок исполнения.

### 8.1 Decomposer (декомпозиция задач)

Минимальная конфигурация: static prefix + обогащение + честный парсинг.

```toml
[agent.decomposer]
name = "decomposer"
prompt_file = "prompts/decompose.txt"          # ~50 токенов, docs/prompts.md
tools = []                                     # декомпозиции инструменты не нужны

init = [
  { plugin = "PromptCompiler" },
  { plugin = "HealthChecker" },
  { plugin = "StatisticsCollector" },
]
input = [
  { plugin = "ContextEnricher", top_k = 3, max_tokens = 500 },
]
trigger = [
  { plugin = "ThinkBlockExtractor" },
]
output = [
  { plugin = "ResponseParser" },               # parse_decomposition
  { plugin = "DuplicateFilter", threshold = 0.75 },
  { plugin = "HistoryLogger" },
]
max_retries = 2
```

Особенности: `is_atomic` проверяется ДО DuplicateFilter; вырожденный повтор
родителя трактуется как атом (protocol.md §3.6) — это делает QualityValidator.

### 8.2 Executor (выполнение атомарных задач)

```toml
[agent.executor]
name = "executor"
prompt_file = "prompts/execute.txt"
tools = ["bash", "file_write", "file_read"]    # MCP-инструменты

init = [
  { plugin = "IndexLoader", source = "mcp" },  # BK-tree инструментов
  { plugin = "PromptCompiler" },
]
input = [
  { plugin = "ContextEnricher", top_k = 5 },
  { plugin = "SafetyInputFilter" },
]
trigger = [
  { plugin = "ThinkBlockExtractor" },
  { plugin = "EarlyStopper" },
]
output = [
  { plugin = "HistoryLogger" },
  { plugin = "SemanticStore" },                 # remember(brief, result, task_id)
]
max_retries = 1
```

### 8.3 Researcher (исследовательская задача)

```toml
[agent.researcher]
name = "researcher"
prompt_file = "prompts/research.txt"           # протокол — docs/process.md §3
tools = ["web_search", "docs_search", "db_query", "file_read", "ast_search"]

init = [
  { plugin = "IndexLoader", source = "mcp" },
]
input = [
  { plugin = "VariableSubstitution" },
]
trigger = [
  { plugin = "ThinkBlockExtractor" },
  { plugin = "LengthGuard", warn_at_tokens = 6000 },
]
output = [
  { plugin = "ResponseParser", parser = "research_findings" },
  { plugin = "HistoryLogger" },
]
max_iterations = 5                               # защита от зацикливания
```

Результат кладётся в `research_cache` (CPU-слой оркестратора, не плагин).

### 8.4 Reflector (обратная связь после провала)

```toml
[agent.reflector]
name = "reflector"
prompt_file = "prompts/reflect.txt"
tools = []

init = []
input = [
  { plugin = "ContextEnricher", include_siblings = true },
]
trigger = [
  { plugin = "ThinkBlockExtractor" },
]
output = [
  { plugin = "ResponseParser", parser = "reflect_decision" },  # retry|reframe|give_up
  { plugin = "HistoryLogger" },
]
max_retries = 1
```

Контракт решения v2 (`retry | reframe | give_up`) — docs/process.md §4;
плагины лишь парсят и валидируют JSON-ответ, применение решает оркестратор.

### 8.5 Таблица сравнения агентов

| | Decomposer | Executor | Researcher | Reflector |
|---|---|---|---|---|
| Инструменты | нет | bash/file | web/docs/db/file/AST | нет |
| Enrichment | да (500 ток) | да (топ-5) | нет (свой скоуп) | да (+siblings) |
| Think-извлечение | да | да | да | да |
| Retry | 2 | 1 | — | 1 |
| SemanticStore | нет | да | нет | нет |
| Изоляция вызова | полный reset | полный reset | полный reset | полный reset |

---

## 9. Жизненный цикл

### 9.1 Последовательность фаз одного run()

```
run(task)
  |
  +-- memory.reset_local()                    # local чист, global живёт
  +-- build messages [system=static prefix, user=brief]
  +-- INPUT plugins   (цепочкой: enrichment -> filters -> trim)
  +-- GENERATION      (стрим токенов)
  |     для каждого токена: TRIGGER plugins (цепочкой)
  |     should_stop / action=stop -> прервать стрим
  |     конец стрима: on_stream_end каждого trigger
  +-- OUTPUT plugins  (парсинг -> дубликаты -> логирование)
  |     action=retry -> повтор ВСЕГО run() (до max_retries)
  |     action=reject -> AgentRejectedError
  +-- return AgentResult(response, parsed, metadata)
```

### 9.2 Тайминги и частоты

| Фаза | Частота | Бюджет времени (референс GTX 1650 Ti) |
|------|---------|----------------------------------------|
| bind()/init | 1 раз за сессию | 0.1–2 c (загрузка индексов) |
| input-плагины | перед каждым вызовом LLM | RAG/semantic: <50 мс; Selective Context: <200 мс |
| trigger на токен | каждый токен стрима | суммарно все плагины < 0.1 мс/токен |
| output-плагины | после генерации | парсинг+детекторы < 20 мс; запись истории < 5 мс |
| retry | по необходимости | полный повтор цикла (дорого! лимит 2) |

Генерация остаётся доминирующей статьёй затрат (30–55 tok/s); плагины не
должны замедлять её заметнее, чем на 5% (проверяется `scripts/benchmark.py`).

### 9.3 Обработка ошибок

| Источник ошибки | Поведение |
|-----------------|-----------|
| Init-плагин упал | bind() падает — агент не создаётся (fail fast) |
| Input-плагин упал | исключение логируется, плагин пропускается, цикл продолжается |
| Trigger-плагин упал | плагин отключается на текущий стрим, остальные работают |
| Output-плагин вернул retry | повтор run(), при исчерпании — AgentRejectedError |
| LLM недоступна | клиент ретраит с backoff; после лимита — TaskFailedError в оркестратор |

Принцип: отказ вспомогательного плагина НЕ должен срывать основной вызов LLM;
отказ критического (ResponseParser) — обязан быть виден, а не проглочен.

---

## 10. Структура проекта

Плагинный слой добавляет в дерево (docs/architecture.md §10.1) следующие модули:

```
src/cascagent/
├── memory.py                      # SessionMemory (§6.1)
├── agent.py                       # Agent, AgentSession, AgentResult (§6)
├── plugins/
│   ├── __init__.py                # реестр PLUGIN_REGISTRY
│   ├── base.py                    # InitPlugin/InputPlugin/TriggerPlugin/OutputPlugin
│   ├── init/
│   │   ├── index_loader.py        # IndexLoader
│   │   ├── prompt_compiler.py     # PromptCompiler
│   │   ├── health_checker.py      # HealthChecker
│   │   └── statistics.py          # StatisticsCollector, RateLimiter
│   ├── input/
│   │   ├── context_enricher.py    # ContextEnricher (обёртка над enricher.py)
│   │   ├── safety_filter.py       # SafetyInputFilter
│   │   ├── history_trimmer.py     # HistoryTrimmer
│   │   └── variables.py           # VariableSubstitution
│   ├── trigger/
│   │   ├── think_extractor.py     # ThinkBlockExtractor
│   │   ├── early_stop.py          # EarlyStopper
│   │   └── pii_filter.py          # PIIFilter
│   └── output/
│       ├── response_parser.py     # ResponseParser
│       ├── duplicate_filter.py    # DuplicateFilter
│       ├── quality_validator.py   # QualityValidator
│       ├── history_logger.py      # HistoryLogger
│       └── semantic_store.py      # SemanticStore
tests/unit/test_memory.py          # SessionMemory: скоупы, подписки, snapshot
tests/unit/test_agent.py           # Agent: bind/run/retry/triggers (fake LLM)
tests/unit/test_plugins_*.py       # по файлу на секцию плагинов
```

Существующие модули (`enricher.py`, `parser.py`, `detector.py`) остаются
единственными реализациями логики — плагины только тонкие обёртки над ними
(CPU-слой не дублируется).

---

## 11. Примеры использования

### 11.1 Hello World через Agent API

```python
from cascagent.agent import Agent
from cascagent.client import LlamaCppClient
from cascagent.plugins.base import InputPlugin, TriggerPlugin, OutputPlugin
from cascagent.plugins.trigger.think_extractor import ThinkBlockExtractor
from cascagent.plugins.output.response_parser import ResponseParser

client = LlamaCppClient(url="http://127.0.0.1:8080/v1")

agent = Agent(
    name="decomposer",
    system_prompt=SYSTEM_PROMPT,            # ~50 токенов из parser.py
    llm_client=client,
    trigger_plugins=[ThinkBlockExtractor()],
    output_plugins=[ResponseParser(parse_decomposition)],
).bind()

result = agent.run(Task(id="0", brief="Написать Hello World на Python"))

if result.parsed == "<atom>":
    print("Задача атомарна -> Executor")
else:
    for sub in result.parsed:
        print(f"- {sub.brief}")
```

Think-блок при этом уже лежит в памяти для честного логирования (§2.4):

```python
think = agent.memory.get_local("think_content")   # str
n = agent.memory.get_local("think_length")        # int
```

### 11.2 Свой input-плагин: подстановка имени проекта

```python
class ProjectVars(InputPlugin):
    name = "project_vars"

    def __init__(self, variables: dict[str, str]):
        self.variables = variables

    def on_input(self, messages, context, session, memory):
        messages[-1]["content"] = messages[-1]["content"].format(**self.variables)
        return messages

# в конфиге: { plugin = "ProjectVars", variables = { project_name = "blog" } }
```

### 11.3 Producer-Consumer: координация Research через память

```python
class ResearchNeededDetector(OutputPlugin):
    """CPU-эвристика: пустой/нерелевантный decompose-ответ -> флаг исследования."""
    name = "research_needed"

    def on_output(self, response, context, session, memory):
        if not context["task"].parent_result and len(response) < 40:
            memory.set_local("needs_research", True)
            memory.set_local("research_query", context["task"].brief)
        return {"response": response, "action": "accept"}

# Оркестратор ПОСЛЕ run() читает флаги (не агенты между собой!):
result = decomposer.run(task)
if decomposer.memory.get_local("needs_research"):
    finding = researcher.run(decomposer.memory.get_local("research_query"))
    research_cache.save(task.brief, finding)
```

### 11.4 Статистика через подписки

```python
class ThinkStats(InitPlugin):
    name = "think_stats"

    def __init__(self):
        self._memory = None          # захватывается в on_init для callback'а

    def on_init(self, session, memory):
        self._memory = memory
        memory.subscribe("response_duration", self._on_done)
        memory.set_global("think_stats", {"calls": 0, "total_ms": 0, "max_ms": 0})
        return session

    def _on_done(self, key, value, scope):
        m = self._memory
        m.update_global("think_stats", lambda s: {
            "calls": s["calls"] + 1,
            "total_ms": s["total_ms"] + value["duration_ms"],
            "max_ms": max(s["max_ms"], value["duration_ms"]),
        })

# после серии run() статистика читается из global-скоупа:
stats = agent.memory.get_global("think_stats")     # {"calls": 12, ...}
snap = agent.memory.snapshot()                     # отладочный дамп памяти
```

Простейшая альтернатива без подписки — прямо в output-плагине:

```python
memory.update_global("total_calls", lambda x: x + 1, default=0)
```

### 11.5 Полная сборка REST API-агента (code, не TOML)

```python
def make_decomposer(client, rag, semantic, kanban) -> Agent:
    return Agent(
        name="decomposer",
        system_prompt=SYSTEM_PROMPT,
        llm_client=client,
        init_plugins=[PromptCompiler(), HealthChecker(client)],
        input_plugins=[ContextEnricher(rag, semantic, kanban, max_tokens=500)],
        trigger_plugins=[ThinkBlockExtractor()],
        output_plugins=[
            ResponseParser(parse_decomposition),
            DuplicateFilter(threshold=0.75),
            HistoryLogger(history),
        ],
    ).bind()
```

---

## 12. Расширение системы

### 12.1 Реестр плагинов и entry points

Все встроенные плагины регистрируются в реестре; конфиг ссылается на плагин
по имени строки:

```python
# src/cascagent/plugins/__init__.py
PLUGIN_REGISTRY: dict[str, type] = {
    "IndexLoader": IndexLoader,
    "PromptCompiler": PromptCompiler,
    "ContextEnricher": ContextEnricher,
    "ThinkBlockExtractor": ThinkBlockExtractor,
    "ResponseParser": ResponseParser,
    # ...
}

def build_plugin(spec: dict) -> object:
    """spec = {"plugin": "ContextEnricher", "top_k": 3, ...}"""
    cls = PLUGIN_REGISTRY[spec["plugin"]]
    kwargs = {k: v for k, v in spec.items() if k != "plugin"}
    return cls(**kwargs)
```

Внешние пакеты могут добавлять плагины через entry point группы
`cascagent.plugins` (pyproject.toml):

```toml
[project.entry-points."cascagent.plugins"]
my_limiter = "mypackage.plugins:TokenBudgetLimiter"
```

Загрузчик дописывает PLUGIN_REGISTRY найденными entry points при старте CLI.

### 12.2 Правила расширения (обязательны к соблюдению)

1. **Плагин не имеет скрытого состояния между run()** — всё состояние только
   в SessionMemory (иначе ломается воспроизводимость §2.1).
2. **Input-плагины не трогают system-сообщение** — статический префикс нужен
   для префиксного кэша llama.cpp; динамика живёт в user.
3. **Trigger-плагины без I/O и аллокаций в горячем пути** — никаких сетевых
   вызовов, записей на диск, эмбеддингов на токен.
4. **Output-плагины идемпотентны** — повторный прогон на том же ответе даёт
   тот же результат (retry полагается на это).
5. **Никаких обращений к другим агентам напрямую** — координация только через
   оркестратор и флаги памяти (§11.3).
6. **Без эвристик атомарности** — `is_atomic` решает модель (`<atom>`),
   QualityValidator проверяет лишь формат (ADR-007).
7. **Логируем честно** — think и final пишутся всегда, даже если downstream
   использует только final (§2.4).
8. **Каждый плагин = unit-тесты с fake LLM/fake memory**, coverage >80%.

### 12.3 Матрица обратной совместимости API

| API | Гарантируется стабильным? | Примечание |
|-----|---------------------------|------------|
| SessionMemory get/set/update/subscribe | да (semver minor) | ключи — domain плагинов |
| Сигнатуры on_init/on_input/on_token/on_output | да (semver major на ломку) | новые optional-параметры — minor |
| Результат trigger `{"action": ...}` | да | новые действия добавляются расширением enum |
| Поля AgentSession | нет | внутренняя структура, может меняться |
| PLUGIN_REGISTRY | да | имена встроенных плагинов не переиспользуются |

### 12.4 Roadmap плагинного слоя

| Версия | Что добавляется |
|--------|-----------------|
| 0.5.x | Этап 5: base-классы + memory + ThinkBlockExtractor/ResponseParser/HistoryLogger; Decomposer переведён на Agent |
| 0.6.x | Researcher/Reflector как конфигурации (§8.3–8.4); EntryPoints для внешних плагинов |
| 0.7.x | SafetyInputFilter/PIIFilter; RateLimiter; multi-turn HistoryTrimmer |
| 1.0.0 | Заморозка API секций; плагины — единственный способ расширения агентов |

---

*Документ описывает архитектуру v3 (плагинный слой). Фактическое поведение
ядра v2 задают: docs/protocol.md (протокол), docs/process.md (конвейер),
docs/data.md (данные), docs/prompts.md (промпты).*

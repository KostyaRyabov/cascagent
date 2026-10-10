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
5. SessionMemory — 4-уровневая память
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

Ключевая установка проекта («LLM — только семантика»), обоснования,
именование и лицензия — **`docs/principles.md` §1, §4** (единственный
источник истины; здесь не пересказываются).

Плагинная архитектура распространяет установку §1 на **код системы**:
агент остаётся тонким «клеем», а вся переиспользуемая логика (обогащение,
парсинг, фильтрация, статистика) выносится в независимые, тестируемые
плагины четырёх секций.

---

## 2. Основные принципы

Архитектурные принципы (изоляция агентов, минимизация контекста, перенос
логики на CPU, минимальный формат, честный парсинг) — в
**`docs/principles.md` §2–§3**. Здесь зафиксированы только следствия для
плагинной архитектуры v3:

- **Изоляция (§2.1 principles)** → каждый вызов агента — отдельный
  HTTP-запрос без истории прошлых вызовов; общая память агентов — только
  Kanban / semantic memory через CPU-слой. Ограничение движка: у
  действующего llama.cpp server нет slot API (ADR-001 пока Proposed),
  поэтому код плагинов **не должен предполагать наличие слотов**;
  slot-изоляция и save/restore KV возможны только при переходе на TabbyAPI.
- **Минимизация контекста (§2.2 principles)** → PromptCompiler компилирует
  системный префикс один раз, а все input-плагины добавляют динамический
  контекст строго в user-сообщение — иначе рвётся префиксный кэш сервера
  (см. §12.2 п.2 и docs/performance.md). Текст промпта Decomposer —
  единственный источник: docs/prompts.md.
- **Честный парсинг (§3 principles)** → в плагинной терминологии:
  ThinkBlockExtractor (trigger) пишет think-блок в local-память,
  HistoryLogger (output) сохраняет ОБА блока — ничего не теряется на
  промежуточных секциях.

### 2.4 Принцип «плагин ≠ бизнес-логика агента»

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
|                        +- super   (все проекты)     |
|                        +- project (все агенты)      |
|                        +- session (этот агент)      |
|                        +- local   (per-run)         |
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
        memory.set_session("tools_loaded", True)
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
    """CPU-обогащение через RAG по эмбеддингу brief'а. LLM НЕ решает что
    искать — поиск идёт по embedding(brief) до передачи контекста модели
    (process.md §2, зафиксировано 2026-10-10)."""

    def on_input(self, messages, context, session, memory):
        task = context["task"]

        # Кэш в local памяти (для multi-turn); детерминирован: одинаковый
        # brief -> одинаковое обогащение
        cache_key = f"enrichment_{task.id}"
        cached = memory.get_local(cache_key)

        if not cached:
            qv = self.embedder.encode(task.brief)      # единый query-вектор
            docs = self.rag.search(qv, top_k=3)        # vector-first (BM25 — предфильтр)
            memories = self.semantic.recall(qv, top_k=3)
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

## 5. SessionMemory — четырёхуровневая память

### 5.1 Концепция уровней

SessionMemory — иерархическое хранилище данных, доступное всем плагинам
агента. Это единственный легальный канал коммуникации между плагинами
(§2.4) и между агентами (через project-уровень). Изоляция (§2.1 principles)
теперь
задаётся **уровнем**, а не запретом разделения: агент по умолчанию видит
только свои local/session; совместные данные явно кладутся на project или
super уровень.

```
+-----------------------------------------------------------+
|                    SUPER GLOBAL                            |
|      Общая для ВСЕХ агентов во ВСЕХ проектах               |
|      Персистентная (переживает перезапуски)                |
|      Примеры: общая статистика, системные конфиги          |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                    PROJECT GLOBAL                          |
|      Общая для всех агентов ВНУТРИ одного проекта          |
|      Персистентная (пока проект активен)                   |
|      Примеры: результаты исследований, общий контекст      |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                    SESSION GLOBAL                          |
|      Приватная для ОДНОЙ сессии агента                     |
|      Персистентная в рамках сессии (дампится)              |
|      Примеры: история диалога, статистика агента           |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                    LOCAL                                   |
|      Приватная для ОДНОГО вызова run()                     |
|      Сбрасывается в начале каждого run()                   |
|      Примеры: think_content, parse_errors, промежуточные   |
+-----------------------------------------------------------+
```

### 5.2 Матрица доступности

| Уровень | Видимость | Персистентность | Сброс |
|---------|-----------|-----------------|-------|
| **local** | Только этот агент в этом run | ❌ нет | Каждый `run()` |
| **session** | Только этот агент во всех run | ✅ дамп/SQLite namespace | При `reset_session()` |
| **project** | Все агенты в проекте | ✅ в БД проекта | При закрытии проекта |
| **super** | Все агенты везде | ✅ в системной БД | Никогда (или вручную) |

Уровни session/project/super реализуются через унифицированный бэкенд
`MemoryStore` (§6.5); session-level даёт изоляцию агентов из §2.1,
project-level — контролируемый обмен (Producer-Consumer, §5.6).

### 5.3 API памяти

```python
class SessionMemory:
    """
    4-уровневая память агента.

    Levels (в порядке приватности):
      local          — только этот run
      session        — вся сессия этого агента
      project        — все агенты в проекте
      super_global   — все агенты везде
    """

    # === LOCAL (per-run, in-memory only) ===
    def set_local(self, key: str, value: Any) -> None: ...
    def get_local(self, key: str, default: Any = None) -> Any: ...
    def update_local(self, key: str, updater: callable, default: Any = None) -> Any: ...

    # === SESSION GLOBAL (per-agent session, persisted) ===
    def set_session(self, key: str, value: Any) -> None: ...
    def get_session(self, key: str, default: Any = None) -> Any: ...
    def update_session(self, key: str, updater: callable, default: Any = None) -> Any: ...

    # === PROJECT GLOBAL (shared across agents in project) ===
    def set_project(self, key: str, value: Any) -> None: ...
    def get_project(self, key: str, default: Any = None) -> Any: ...
    def update_project(self, key: str, updater: callable, default: Any = None) -> Any: ...

    # === SUPER GLOBAL (shared across all projects) ===
    def set_super(self, key: str, value: Any) -> None: ...
    def get_super(self, key: str, default: Any = None) -> Any: ...
    def update_super(self, key: str, updater: callable, default: Any = None) -> Any: ...

    # === УПРАВЛЕНИЕ ===
    def reset_local(self) -> None: ...    # вызывается автоматически в начале run()
    def reset_session(self) -> None: ...  # полный сброс сессии агента (local + session)

    # === ПОДПИСКИ (event-driven) ===
    def subscribe(self, key: str, callback: callable) -> None: ...

    # === УТИЛИТЫ ===
    def snapshot(self) -> dict: ...       # снимок всех четырёх уровней
```

Именование методов (`*_session`, `*_project`, `*_super`) фиксируется как
API v3; старые `*_global` из черновиков не поддерживаются (см. §5.6–§5.7).

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

### 5.5 Жизненный цикл и создание памяти

Память создаётся фабрикой до `bind()` — один экземпляр SessionMemory на
агента; оркестратор передаёт его в `Agent(memory=...)`:

```python
# При старте системы
factory = MemoryFactory(
    project_id="blog-api-project",
    super_global_db=Path("./data/super_global.db"),
    projects_db=Path("./data/projects.db"),
)

# Для каждого агента в проекте
decomposer_memory = factory.create_for_agent(
    agent_name="decomposer",
    session_id="decomposer-session-2026-10-10-1",
)
executor_memory = factory.create_for_agent(
    agent_name="executor",
    session_id="executor-session-2026-10-10-1",
)

# decomposer и executor делят project-level память,
# но их session-level хранилища изолированы
```

Namespace'ы (`MemoryFactory.create_for_agent`, код — §6.6):

| Уровень | БД | namespace |
|---------|----|-----------| 
| session | projects.db | `session:{session_id}` |
| project | projects.db | `project:{project_id}` |
| super | super_global.db | `system` |

Видимость между агентами:

```python
# Decomposer Agent пишет на общий уровень
decomposer_memory.set_project("research_jwt_findings", {...})

# Executor Agent может прочитать
jwt_info = executor_memory.get_project("research_jwt_findings")

# Но НЕ видит session-level decomposer'а — это СВОЙ счётчик executor'а:
calls = executor_memory.get_session("calls_count")   # → None
```

### 5.6 Паттерны использования

**Producer-Consumer внутри агента (Trigger → Output):**

```python
# Trigger плагин пишет в local
memory.set_local("think_content", think_block)

# Output плагин читает
think = memory.get_local("think_content", "")
```

**Producer-Consumer между агентами (Researcher → Executor):**

```python
# Researcher Agent (producer) — результат исследования в проект
memory.set_project("research_stripe_api", {
    "version": "2025-10",
    "auth": "Bearer token",
    "endpoints": {...},
})

# Executor Agent (consumer)
stripe_info = memory.get_project("research_stripe_api")
```

**Счётчики на уровне session (атомарное обновление):**

```python
memory.update_session("calls_count", lambda x: x + 1, default=0)
memory.update_session("tokens_used", lambda x: x + tokens, default=0)

avg = memory.get_session("tokens_used") / memory.get_session("calls_count")
```

**Глобальная статистика на super-уровне (редко!):**

```python
# В output-плагине любого агента
memory.update_super(
    "global_stats",
    lambda s: {
        "total_calls": s["total_calls"] + 1,
        "total_duration_ms": s["total_duration_ms"] + duration,
        "total_tokens": s["total_tokens"] + tokens,
    },
    default={"total_calls": 0, "total_duration_ms": 0, "total_tokens": 0},
)
```

**Кэш в рамках run() (local):**

```python
cached = memory.get_local(f"enrichment_{task.id}")
if not cached:
    cached = expensive_computation()
    memory.set_local(f"enrichment_{task.id}", cached)
```

**Флаги координации (local, читает оркестратор после run — §11.3):**

```python
# Плагин A
memory.set_local("needs_research", True)
memory.set_local("research_query", "как работает X")

# Плагин B (позже в цепочке output)
if memory.get_local("needs_research"):
    query = memory.get_local("research_query")
    # запустить исследование
```

### 5.7 Соглашения об именовании ключей

| Префикс ключа | Уровень | Пример | Кто пишет |
|---------------|---------|--------|-----------|
| `think_*` | local | `think_content`, `think_length` | Trigger |
| `enrichment_*` | local | `enrichment_0.2.1` | Input |
| `parsed`, `parse_error` | local | результат парсинга | Output |
| `needs_*`, флаги | local | `needs_research` | любой |
| `*_stats`, `calls_count` | session | `think_stats`, `conversation_history` | StatisticsCollector |
| `rate_limit_calls` | session | счётчики лимитов агента | Init/Output |
| `research_*`, `task_*_result` | project | findings, результаты задач | Output/оркестратор |
| `tasks` | project | иерархия задач `{id: Task}` после декомпозиции (process §1.3, data.md §1) | Output (TaskParser) |
| `global_stats`, `system_version` | super | сквозная статистика | MetricsWriter |

Ключи без префикса — локальные переменные одного плагина, другим секциям
не гарантируются. Правило выбора уровня: по умолчанию пишется на самый
приватный подходящий уровень; повышение уровня — осознанное решение
(лидер проекта: super-уровень использовать только для статистики и
системных конфигов).

### 5.8 Breakpoints и recovery (решение аудита #3)

Пересмотр черновика roadmap §3.3: отдельные `_stats`/`_breakpoints` поля
**не вводятся** — их роль полностью берут на себя session/project уровни
(они персистентны по построению, §5.2). Остаются только точки
восстановления:

```python
class BreakpointManager:
    """Сохраняет снапшоты состояния для восстановления."""

    def __init__(self, memory: SessionMemory, storage_path: Path): ...

    def save_breakpoint(self, label: str, context: dict = None):
        """append JSONL-записи {label, timestamp, context, snapshot()}"""

    def load_breakpoint(self, label: str) -> dict | None:
        """последняя точка с таким label"""

    def restore_from(self, breakpoint: dict):
        """local пересоздаётся из снапшота; session восстанавливается
        целиком; project/super НЕ трогаем — они персистентные и могли
        измениться другими агентами пока процесс был мёртв"""
```

Правила:

- `reset_local()` не трогает session/project/super; `reset_session()`
  чистит local + session (этот агент начинает новую сессию).
- Breakpoint'ы пишутся в `data/breakpoints/{session_id}.jsonl` плагином
  `AutoCheckpoint` (Output) после каждого успешного run и при FAILED
  задачах; ротация — хранить 20 последних на сессию (gzip, открытый
  вопрос roadmap §9.1 теперь решён в пользу JSONL+gzip).
- Подписки (§5.4) работают на все четыре уровня; callback получает
  `(key, value, level)`.
- Механика `cascagent resume` (kanban как источник истины о задачах) —
  `docs/roadmap.md` §3.3; память дополняет её состоянием агента.
- Логику MemoryEntry/актуальности semantic memory (#8) см. roadmap §4.4 —
  это уровень `semantic.py`, а не SessionMemory.

### 5.9 Гарантии и ограничения

**Гарантии:**

1. Атомарность записи: каждая операция set/update завершается полностью
   или не выполняется (SQLite-транзакция на запись).
2. Персистентность: session/project/super переживают перезапуск процесса.
3. Изоляция local: не видна другим run, другим агентам, другим процессам.
4. Отсутствие коллизий: одинаковые ключи на разных уровнях — разные данные.
5. Последовательность: всё однопоточно, никаких race conditions.

**Ограничения (принятые сознательно):**

1. Нет распределённых транзакций: запись в project и super — отдельные
   операции.
2. Нет блокировок: не нужны (нет параллелизма, MVP однопоточный).
3. Нет TTL: очистка только через cleanup-плагины (`SessionCleanup`, §7.1).
4. Нет версионирования записей: если надо — делает плагин (например,
   version-поле в значении).
5. Подписка не фильтруется по уровню: callback вызывается на изменение
   ключа на ЛЮБОМ уровне (уровень приходит третьим аргументом).

---

## 6. Базовые классы

Полный код базовых классов для расширения. Реализуются в модулях
`src/cascagent/plugins/base.py` (классы плагинов и Agent) и
`src/cascagent/memory.py` (SessionMemory).

### 6.1 SessionMemory

```python
from typing import Any, Callable
from pathlib import Path


class SessionMemory:
    """4-уровневая память для плагинов (§5)."""

    def __init__(
        self,
        session_store: MemoryStore,       # PersistentStore, namespace сессии
        project_store: MemoryStore,       # PersistentStore, namespace проекта
        super_global_store: MemoryStore,  # PersistentStore, namespace "system"
    ):
        self._local: dict[str, Any] = {}
        self._session = session_store
        self._project = project_store
        self._super = super_global_store
        self._subscribers: dict[str, list[Callable]] = {}

    # === LOCAL (per-run, in-memory only) ===

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

    # === SESSION GLOBAL (per-agent session, persisted) ===

    def set_session(self, key: str, value: Any):
        self._session.set(key, value)
        self._notify(key, value, "session")

    def get_session(self, key: str, default: Any = None) -> Any:
        return self._session.get(key, default)

    def update_session(self, key: str, updater: Callable, default: Any = None) -> Any:
        new_value = self._session.update(key, updater, default)
        self._notify(key, new_value, "session")
        return new_value

    # === PROJECT GLOBAL (shared across agents in project) ===

    def set_project(self, key: str, value: Any):
        self._project.set(key, value)
        self._notify(key, value, "project")

    def get_project(self, key: str, default: Any = None) -> Any:
        return self._project.get(key, default)

    def update_project(self, key: str, updater: Callable, default: Any = None) -> Any:
        new_value = self._project.update(key, updater, default)
        self._notify(key, new_value, "project")
        return new_value

    # === SUPER GLOBAL (shared across all projects) ===

    def set_super(self, key: str, value: Any):
        self._super.set(key, value)
        self._notify(key, value, "super")

    def get_super(self, key: str, default: Any = None) -> Any:
        return self._super.get(key, default)

    def update_super(self, key: str, updater: Callable, default: Any = None) -> Any:
        new_value = self._super.update(key, updater, default)
        self._notify(key, new_value, "super")
        return new_value

    # === УПРАВЛЕНИЕ ===

    def reset_local(self):
        self._local.clear()

    def reset_session(self):
        """Полный сброс сессии агента (project/super не трогаем)."""
        self._local.clear()
        self._session.clear()

    # === ПОДПИСКИ ===

    def subscribe(self, key: str, callback: Callable):
        if key not in self._subscribers:
            self._subscribers[key] = []
        self._subscribers[key].append(callback)

    def _notify(self, key: str, value: Any, level: str):
        for cb in self._subscribers.get(key, []):
            try:
                cb(key, value, level)
            except Exception as e:
                print(f"[memory] subscriber error for {key}: {e}")

    def snapshot(self) -> dict:
        """Снимок всей памяти для дампинга/отладки/breakpoint'ов."""
        return {
            "local": dict(self._local),
            "session": self._session.all(),
            "project": self._project.all(),
            "super": self._super.all(),
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


> **v3 (2026-10-11, решения лида).** Секции переименованы: InitPlugin убран
> (его роль — ленивая тяжёлая подготовка в PrePlugin), InputPlugin → PrePlugin,
> TriggerPlugin → RuntimePlugin, OutputPlugin → PostPlugin. Из API убраны
> `reset()` (очистка — ответственность агента: `run()` стирает local-скоуп
> памяти), `should_stop` и `on_stream_end` (семантика останова живёт в op-типах
> `on_token`; финализация потока — работа PostPlugin). Память биндится в плагин
> ОДИН РАЗ (`bind()`), а не прокидывается аргументом в каждый хук — хот-путь
> на токен не тащит лишние параметры. Хуки работают с двумя скоупами:
> `self.global_memory` (переживает итерации) и `self.local_memory`
> (очищается агентом перед каждым `run()`).

```python
class Plugin(ABC):                     # общий предок: name/config/bind()
    ...

class PrePlugin(Plugin):               # секция PRE (был InputPlugin)
    name = "base_pre"

    @abstractmethod
    def on_input(self, messages: list, context: dict) -> list:
        """Меняет ТОЛЬКО user-сообщения; system/assistant — байт-в-байт
        (префиксный кэш llama.cpp, P8)."""

class RuntimePlugin(Plugin):           # секция RUNTIME (был TriggerPlugin)
    name = "base_runtime"

    def on_token(self, token: str, accumulated: str) -> dict:
        """{"op": "continue"} | {"op": "stop", "data": ...}
        (останов без ошибки) | {"op": "error", "data": "..."}
        (останов с ошибкой). Дефолт: pass-through."""
        return {"op": "continue"}

class PostPlugin(Plugin):              # секция POST (был OutputPlugin)
    name = "base_post"

    @abstractmethod
    def on_output(self, response: str, context: dict) -> dict:
        """{"response", "parsed", "metadata",
           "action": accept|retry|reject}."""
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

    def bind(self, memory: SessionMemory | None = None) -> "Agent":
        """Инициализация сессии. Вызывается ровно один раз.

        memory — готовая 4-уровневая память из MemoryFactory (§5.5);
        если не передана — создаётся изолированная (все store'ы in-memory,
        удобно в тестах).
        """
        if self.session is not None:
            raise RuntimeError("Agent already bound")

        self.memory = memory or SessionMemory(
            session_store=InMemoryStore(),
            project_store=InMemoryStore(),
            super_global_store=InMemoryStore(),
        )
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

### 6.5 MemoryStore — унифицированный бэкенд

Все персистентные уровни памяти строятся на одном абстрактном KV-хранилище
(`src/cascagent/memory.py`):

```python
import json
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable


class MemoryStore(ABC):
    """Абстрактное хранилище ключ-значение."""

    @abstractmethod
    def set(self, key: str, value: Any) -> None: ...

    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any: ...

    def update(self, key: str, updater: Callable, default: Any = None) -> Any:
        current = self.get(key, default)
        new_value = updater(current)
        self.set(key, new_value)
        return new_value

    @abstractmethod
    def all(self) -> dict: ...

    @abstractmethod
    def clear(self) -> None: ...


class InMemoryStore(MemoryStore):
    """Простое in-memory хранилище (тесты, изолированные агенты)."""

    def __init__(self):
        self._data: dict[str, Any] = {}

    def set(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)

    def all(self):
        return dict(self._data)

    def clear(self):
        self._data.clear()


class PersistentStore(MemoryStore):
    """
    SQLite-backed хранилище. Для session/project/super уровней.
    Сериализует значения через JSON; разделение по namespace в одной таблице.
    """

    def __init__(self, db_path: Path, namespace: str):
        self.db_path = db_path
        self.namespace = namespace
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, key)
            )
        """)
        conn.commit()
        conn.close()

    def set(self, key, value):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT OR REPLACE INTO memory (namespace, key, value, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (self.namespace, key, json.dumps(value, ensure_ascii=False))
        )
        conn.commit()
        conn.close()

    def get(self, key, default=None):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT value FROM memory WHERE namespace = ? AND key = ?",
            (self.namespace, key)
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else default

    def all(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT key, value FROM memory WHERE namespace = ?",
            (self.namespace,)
        ).fetchall()
        conn.close()
        return {k: json.loads(v) for k, v in rows}

    def clear(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM memory WHERE namespace = ?", (self.namespace,))
        conn.commit()
        conn.close()
```

Замечания по реализации:

- Одно соединение на операцию достаточно при однопоточном MVP (§5.9);
  при переходе на параллельных агентов — connection pool и WAL-режим.
- Таблица `memory` уживается с kanban-схемами (`docs/data.md`) в одной
  БД проекта; для super уровня — отдельный файл `super_global.db`.
- Значения должны быть JSON-сериализуемы; сложные объекты (эмбеддинги)
  хранятся в semantic memory (`semantic.py`), не здесь.

### 6.6 MemoryFactory

```python
class MemoryFactory:
    """Создаёт SessionMemory с правильными store'ами (§5.5)."""

    def __init__(
        self,
        project_id: str,
        super_global_db: Path = Path("./data/super_global.db"),
        projects_db: Path = Path("./data/projects.db"),
    ):
        self.project_id = project_id
        self.super_global_db = super_global_db
        self.projects_db = projects_db

    def create_for_agent(self, agent_name: str, session_id: str) -> SessionMemory:
        """
        - local:     plain dict (приватный, не персистентный)
        - session:   PersistentStore projects.db, namespace=session:{session_id}
        - project:   PersistentStore projects.db, namespace=project:{project_id}
        - super:     PersistentStore super_global.db, namespace="system"
        """
        return SessionMemory(
            session_store=PersistentStore(
                self.projects_db, namespace=f"session:{session_id}"
            ),
            project_store=PersistentStore(
                self.projects_db, namespace=f"project:{self.project_id}"
            ),
            super_global_store=PersistentStore(
                self.super_global_db, namespace="system"
            ),
        )
```

### 6.7 BreakpointManager

```python
class BreakpointManager:
    """Точки восстановления (§5.8). Формат — JSONL, append-only."""

    def __init__(self, memory: SessionMemory, storage_path: Path):
        self.memory = memory
        self.storage = storage_path

    def save_breakpoint(self, label: str, context: dict | None = None):
        bp = {
            "label": label,
            "timestamp": datetime.now().isoformat(),
            "context": context or {},
            "snapshot": self.memory.snapshot(),
        }
        self.storage.parent.mkdir(parents=True, exist_ok=True)
        with open(self.storage, "a", encoding="utf-8") as f:
            f.write(json.dumps(bp, ensure_ascii=False) + "\n")

    def load_breakpoint(self, label: str) -> dict | None:
        if not self.storage.exists():
            return None
        last = None
        with open(self.storage, encoding="utf-8") as f:
            for line in f:
                bp = json.loads(line)
                if bp["label"] == label:
                    last = bp          # берём ПОСЛЕДНЕЕ совпадение
        return last

    def restore_from(self, breakpoint: dict):
        """local пересоздаётся; session восстанавливается целиком;
        project/super НЕ трогаем (персистентны, могли измениться другими
        агентами пока процесс был мёртв)."""
        snap = breakpoint["snapshot"]
        self.memory.reset_local()
        for k, v in snap.get("local", {}).items():
            self.memory.set_local(k, v)
        self.memory._session.clear()
        for k, v in snap.get("session", {}).items():
            self.memory.set_session(k, v)
```

---

## 7. Каталог встроенных плагинов

### 7.1 Init-плагины

| Плагин | Назначение |
|--------|-----------|
| `IndexLoader` | Загружает BK-tree инструментов в `session.indices` |
| `PromptCompiler` | Прекомпилирует system prompt + few-shot в статический префикс |
| `HealthChecker` | Проверяет доступность LLM-эндпоинта (GET /v1/models), без генерации |
| `StatisticsCollector` | Подписывается на события памяти для сбора статистики (session-уровень) |
| `RateLimiter` | Инициализирует счётчики rate limiting в session памяти |
| `CheckpointLoader` | При bind() загружает последний breakpoint (`resume`) в память агента |
| `SessionCleanup` | При старте чистит старые session-namespace'ы (config: `[memory.cleanup]`) |
| `TraceExporter` | Открывает trace.jsonl / соединяется с OTLP-collector (опционально, #6) |

### 7.2 Input-плагины

| Плагин | Назначение |
|--------|-----------|
| `ContextEnricher` | CPU RAG по embedding(brief'а) + semantic memory + siblings; LLM не решает что искать (docs/process.md §2) |
| `SafetyInputFilter` | Маскирует PII (email, карты, API keys) во входе |
| `PromptInjectionGuard` | Санитизирует данные из RAG/memory, оборачивает их в data-блоки, детектит инъективные паттерны (#2, roadmap §3.2) |
| `HistoryTrimmer` | Обрезает длинную историю для multi-turn агентов |
| `VariableSubstitution` | Подставляет `{project_name}`, `{date}` |
| `RateLimitCheck` | Проверяет и применяет rate limiting |
| `ConversationHistory` | Добавляет историю диалога из session памяти |

### 7.3 Trigger-плагины

| Плагин | Назначение |
|--------|-----------|
| `ThinkBlockExtractor` | Разделяет think/final на лету, пишет think в local память |
| `EarlyStopper` | Останавливает генерацию по стоп-маркерам (например, маркер конца ответа чата) |
| `FirstTokenTimeout` | Прерывает стрим, если первый токен не пришёл за N сек (#1) |
| `TotalResponseTimeout` | Прерывает стрим по общему бюджету времени ответа (#1) |
| `StuckDetector` | Детект зацикливания/бесконечного think по повторам в accumulated (#5) |
| `PIIFilter` | Маскирует чувствительные данные на лету |
| `TokenLogger` | Логирует прогресс генерации в debug.log (с троттлингом записи) |
| `LengthGuard` | Предупреждает о приближении к limit контекстного окна |

### 7.4 Output-плагины

| Плагин | Назначение |
|--------|-----------|
| `TaskParser` (`ResponseParser`) | Единый конвейер пост-обработки decompose-ответа (process §1.3), три стадии в одном проходе: **парсинг** protocol §3 (блоки «Название / Описание» через пустую строку, двухуровневый `<atom>`) → `Task[]` (все поля — в `__init__`: created_at → snowflake id, `embedding = embed(brief)`, depth от parent; порядок детей = позиция в списке, поля `order` нет); **фильтрация/проверка** F1–F3 (F1 brief==description ⇒ атом; F2 семантическая близость brief ребёнка к brief родителя ≥ порога ⇒ ребёнок удаляется; F3 у родителя не осталось детей ⇒ родитель становится атомом) по эмбеддингам стадии 1, без повторного кодирования; **сохранение** — подставляет отфильтрованное поддерево детей нужному родителю в уже заполненный project-ключ `tasks` (`parent.subtasks = List[Task]`, прямые ссылки; не перезаписывает дерево целиком); атом ⇒ `description=None`, `is_atom=True` |
| `DuplicateFilter` | Удаляет дубликаты подзадач (DuplicateDetector, порог 0.75) |
| `QualityValidator` | Проверки: пустой ответ, вырожденный повтор родителя, мусор |
| `RefusalDetector` | Детект отказа модели → action=retry с reframe или ошибка (#5) |
| `ExecutionTimeout` | Таймаут выполнения кода/команд инструмента (Executor, #1) |
| `AutoCheckpoint` | Сохраняет breakpoint после успешного run / FAILED задачи (§5.8, #3) |
| `HistoryLogger` | Пишет DecompositionCall в JSONL + THINK/FINAL в debug.log |
| `SemanticStore` | Сохраняет результат в semantic memory (remember) |
| `MetricsCollector` | Собирает токены/время в session-статистику, сквозные суммы — в super |
| `MetricsWriter` | Дублирует метрики в metrics.jsonl для внешних дашбордов (#6) |
| `QualityScorer` | CPU-метрики декомпозиции: specificity, non-redundancy, atom-ratio (#15) |

Все новые плагины из аудита — опциональные: подключаются списком в TOML
(§8), по умолчанию в конфигурациях агентов их нет. Полная привязка
«пробел аудита → плагин» — `docs/roadmap.md` §7.

---

## 8. Конфигурации агентов

Агенты описываются в TOML (см. `docs/config.md`): список секций и параметров
плагинов. Порядок плагинов в списке = порядок исполнения.

### 8.1 Decomposer (декомпозиция задач)

Минимальная конфигурация: static prefix + обогащение + честный парсинг.

```toml
[agent.decomposer]
name = "decomposer"
prompt_file = "prompts/decompose.txt"          # ~110 токенов, docs/prompts.md
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
  { plugin = "TaskParser", threshold = 0.75 }, # парсинг → F1–F3 → подставка поддерева в tasks (process §1.3)
  { plugin = "DuplicateFilter", threshold = 0.75 },
  { plugin = "HistoryLogger" },
]
max_retries = 2
```

Особенности: `is_atom` проверяется ДО DuplicateFilter; вырожденный повтор
родителя трактуется как атом (protocol.md §3.7) — это делает TaskParser
(F2/F3) по эмбеддингам стадии парсинга, QualityValidator ловит остальной
мусор. Дерево задач живёт в project-ключе `tasks` (§5.7); плагин
подставляет готовое поддерево к родителю, не перезаписывая ключ.

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
| Изоляция вызова | reset_local() | reset_local() | reset_local() | reset_local() |

### 8.6 Конфигурация памяти

Общая для всех агентов проекта (в `config.toml`, см. `docs/config.md`):

```toml
[memory]
super_global_db = "./data/super_global.db"
projects_db = "./data/projects.db"        # session + project namespace'ы
breakpoints_dir = "./data/breakpoints"

[memory.auto_checkpoint]                  # плагин AutoCheckpoint (§7.4)
enabled = true
save_after_every_task = true
max_breakpoints_per_session = 20          # ротация gzip-архива JSONL

[memory.cleanup]                          # плагин SessionCleanup (§7.1)
auto_cleanup_on_start = true
max_session_age_days = 30
```

`MemoryFactory` (§6.6) строится из этих значений при старте CLI; session_id
формируется как `{agent_name}-{start_timestamp}` и попадает в breakpoint-файл
и trace (#4, #6).

---

## 9. Жизненный цикл

### 9.1 Последовательность фаз одного run()

```
run(task)
  |
  +-- memory.reset_local()          # local чист; session/project/super живут
  +-- build messages [system=static prefix, user=brief]
  +-- INPUT plugins   (цепочкой: enrichment -> filters -> trim)
  |     читают project/session/super, пишут local (промежуточное)
  +-- GENERATION      (стрим токенов)
  |     для каждого токена: TRIGGER plugins (цепочкой)
  |     should_stop / action=stop -> прервать стрим
  |     конец стрима: on_stream_end каждого trigger
  +-- OUTPUT plugins  (парсинг -> дубликаты -> логирование)
  |     читают local; пишут session (своя статистика/история),
  |     project (результаты для других агентов), super (сквозные суммы);
  |     AutoCheckpoint сохраняет breakpoint (§5.8)
  |     action=retry -> повтор ВСЕГО run() (до max_retries)
  |     action=reject -> AgentRejectedError
  +-- return AgentResult(response, parsed, metadata)
      # local будет очищен в начале следующего run
```

Тайминг-оверхед памяти: local — наносекунды (dict); session/project/super —
SQLite-запись на ключ (<1 мс локально); горячий путь триггеров использует
только local (§12.2 п.3).

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
├── memory.py                      # SessionMemory, MemoryStore, InMemoryStore,
│                                  # PersistentStore, MemoryFactory (§6.1, §6.5, §6.6)
├── breakpoints.py                 # BreakpointManager (§6.7)
├── agent.py                       # Agent, AgentSession, AgentResult (§6)
├── plugins/
│   ├── __init__.py                # реестр PLUGIN_REGISTRY
│   ├── base.py                    # InitPlugin/InputPlugin/TriggerPlugin/OutputPlugin
│   ├── init/
│   │   ├── index_loader.py        # IndexLoader
│   │   ├── prompt_compiler.py     # PromptCompiler
│   │   ├── health_checker.py      # HealthChecker
│   │   ├── checkpoint_loader.py   # CheckpointLoader
│   │   ├── session_cleanup.py     # SessionCleanup
│   │   └── statistics.py          # StatisticsCollector, RateLimiter
│   ├── input/
│   │   ├── context_enricher.py    # ContextEnricher (обёртка над enricher.py)
│   │   ├── safety_filter.py       # SafetyInputFilter
│   │   ├── injection_guard.py     # PromptInjectionGuard
│   │   ├── history_trimmer.py     # HistoryTrimmer
│   │   └── variables.py           # VariableSubstitution
│   ├── trigger/
│   │   ├── think_extractor.py     # ThinkBlockExtractor
│   │   ├── early_stop.py          # EarlyStopper
│   │   ├── timeouts.py            # FirstTokenTimeout, TotalResponseTimeout
│   │   ├── stuck_detector.py      # StuckDetector
│   │   └── pii_filter.py          # PIIFilter
│   └── output/
│       ├── response_parser.py     # ResponseParser
│       ├── duplicate_filter.py    # DuplicateFilter
│       ├── quality_validator.py   # QualityValidator (+RefusalDetector)
│       ├── auto_checkpoint.py     # AutoCheckpoint
│       ├── history_logger.py      # HistoryLogger
│       ├── semantic_store.py      # SemanticStore
│       └── metrics.py             # MetricsCollector, MetricsWriter, QualityScorer
tests/unit/test_memory.py          # 4 уровня: set/get/update, изоляция session/project,
                                   # подписки, snapshot; PersistentStore на tmp SQLite
tests/unit/test_breakpoints.py     # save/load/restore, project/super не трогаются
tests/unit/test_agent.py           # Agent: bind(memory)/run/retry/triggers (fake LLM)
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
    system_prompt=SYSTEM_PROMPT,            # ~110 токенов из parser.py
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
        memory.set_session("think_stats", {"calls": 0, "total_ms": 0, "max_ms": 0})
        return session

    def _on_done(self, key, value, scope):
        m = self._memory
        m.update_session("think_stats", lambda s: {
            "calls": s["calls"] + 1,
            "total_ms": s["total_ms"] + value["duration_ms"],
            "max_ms": max(s["max_ms"], value["duration_ms"]),
        })

# после серии run() статистика читается из session-уровня:
stats = agent.memory.get_session("think_stats")     # {"calls": 12, ...}
snap = agent.memory.snapshot()                     # отладочный дамп памяти
```

Простейшая альтернатива без подписки — прямо в output-плагине:

```python
memory.update_session("calls_count", lambda x: x + 1, default=0)
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
   оркестратор и флаги памяти (§11.3) или project-уровень SessionMemory
   (Producer-Consumer, §5.6).
6. **Данные кладутся на самый приватный подходящий уровень** (§5.7): local →
   session → project → super; запись в project/super из input/trigger
   запрещена (только output и init).
7. **Без эвристик атомарности** — `is_atom` решает модель (`<atom>`),
   QualityValidator проверяет лишь формат (ADR-007). Единственные
   детерминированные CPU-исключения — правила F1–F3 плагина TaskParser
   (process §1.3: degenerate-repeat родителя и brief==description);
   они не «угадывают» атомарность текста, а реализуют правило protocol §3.7.
8. **Логируем честно** — think и final пишутся всегда, даже если downstream
   использует только final (§2.4).
9. **Каждый плагин = unit-тесты с fake LLM/fake memory**, coverage >80%.
   Fake-память — SessionMemory со всеми InMemoryStore (§6.5).

### 12.3 Матрица обратной совместимости API

| API | Гарантируется стабильным? | Примечание |
|-----|---------------------------|------------|
| SessionMemory get/set/update по 4 уровням + subscribe/snapshot | да (semver minor) | ключи — domain плагинов; `*_global` не вводятся |
| MemoryStore / PersistentStore API (set/get/update/all/clear) | да (semver minor) | схема таблицы `memory(namespace,key,value,updated_at)` — стабильна |
| BreakpointManager JSONL-формат | нет до 1.0.0 | при смене формата — версионирование записи `v` |
| Сигнатуры on_init/on_input/on_token/on_output | да (semver major на ломку) | новые optional-параметры — minor |
| Результат trigger `{"action": ...}` | да | новые действия добавляются расширением enum |
| Поля AgentSession | нет | внутренняя структура, может меняться |
| PLUGIN_REGISTRY | да | имена встроенных плагинов не переиспользуются |

### 12.4 Roadmap плагинного слоя

| Версия | Что добавляется |
|--------|-----------------|
| 0.5.x | Этап 5: base-классы + memory (local+session on InMemoryStore) + ThinkBlockExtractor/ResponseParser/HistoryLogger; Decomposer переведён на Agent |
| 0.5.x+ | Персистентность: PersistentStore + MemoryFactory (project/session namespace'ы); super-уровень и AutoCheckpoint/BreakpointManager — вместе с #3 из roadmap (§8 чек-лист) |
| 0.6.x | Researcher/Reflector как конфигурации (§8.3–8.4); EntryPoints для внешних плагинов |
| 0.7.x | SafetyInputFilter/PIIFilter; RateLimiter; multi-turn HistoryTrimmer |
| 1.0.0 | Заморозка API секций; плагины — единственный способ расширения агентов |

---

*Документ описывает архитектуру v3 (плагинный слой). Фактическое поведение
ядра v2 задают: docs/protocol.md (протокол), docs/process.md (конвейер),
docs/data.md (данные), docs/prompts.md (промпты).*

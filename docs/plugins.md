# cascagent — Плагинная архитектура агентов

> **Статус:** 🟡 частично реализовано (v0.1). Реализованы и покрыты тестами:
> базовые классы секций (§6.3, факт: `plugins/base.py` — Pre/Runtime/Post,
> Init убран), плоская KV-память `Memory` + иерархия Workspace/Project/Agent
> и оркестратор `Agent.run()` (§6.4 в фактическом виде, `memory.py`),
> встроенный сервис `StreamParser` (`stream_parser.py`, спека
> `docs/stream_parser_model.md`) и единственный конкретный плагин
> `TaskParserPlugin` (`plugins/task_parser.py`). НЕ реализованы (план):
> персистентность памяти (§5.2/§6.5 PersistentStore/MemoryFactory),
> BreakpointManager (§5.8/§6.7), каталог плагинов §7, клиент LLM, канбан,
> CLI-оркестратор. Остальное ниже — спецификация целевого состояния.
> Смежные документы: `docs/architecture.md` (обзор модулей),
> `docs/protocol.md` (протокол декомпозиции), `docs/process.md` (конвейер
> обогащения контекста), `docs/prompts.md` (тексты промптов),
> `docs/config.md` (формат конфигурации агентов).

---

## Содержание

1. Миссия и философия
2. Основные принципы
3. Архитектура агента
4. Секции плагинов (Pre / Runtime / Post)
5. Память — четыре уровня иерархии
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
только через разделяемую память (§5).

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

Исключение из правила «вся логика в плагинах» (решение 2026-10-12):
структурирование потока — не плагин, а **встроенный сервис агента**
`StreamParser` (`stream_parser.py`, спека `docs/stream_parser_model.md`).
Он не расширяем конфигом и отсутствует в реестре плагинов.

### 3.2 Структура агента

```
+-----------------------------------------------------+
|                     AGENT (memory.Agent)            |
|                                                     |
|  llm: callable(messages) -> str | chunk iterator    |
|  parser_config: dict  -> StreamParser per run()     |
|                                                     |
|  [PRE plugins]      [RUNTIME plugins]   [POST]      |
|                                                     |
|  global_memory (переживает итерации)                |
|  local_memory  (трётся каждым run())                |
|   ^ project_memory / workspace_memory — ссылки вверх|
+-----------------------------------------------------+
```

Память — четыре раздельных плоских KV-экземпляра `Memory` (не один
четырёхскоуповый объект; эволюция модели — §5.1):

- **workspace** — всё во всех проектах (персистентность — план);
- **project** — все агенты проекта (дерево задач `tasks` живёт здесь);
- **global** — состояние одного агента между итерациями;
- **local** — один прогон `run()`, общие данные плагинов одного цикла.

### 3.3 Состояния агента

1. **Unbound** — только создан, плагины не инициализированы;
2. **Bound** — прошёл через init-плагины, готов к работе;
3. **Running** — выполняет run(task), использует память.

В фактической реализации (v0.1) разделение Unbound/Bound снято:
`Agent.add_plugin()` сразу биндит все четыре памяти в плагин
(`Plugin.bind()`), отдельного этапа `bind(memory)` нет — связывание
происходит один раз при регистрации, а не перед каждым вызовом хука.

```python
agent = Agent(...)         # Unbound
result = agent.run(messages, context)   # Bound+Running: bind — в add_plugin()
```

Историческое правило «повторный bind() запрещён» снято вместе с этапом
bind(): состояние агента между вызовами живёт в `global_memory`, новый
запуск — новый `run()` (или новый экземпляр для полной изоляции).

---

## 4. Секции плагинов (Pre / Runtime / Post)

Переименование 2026-10-11 (решения лида): InitPlugin **удалён** (его роль —
ленивая тяжёлая подготовка в PrePlugin под флагом в global-памяти),
InputPlugin → **PrePlugin**, TriggerPlugin → **RuntimePlugin**,
OutputPlugin → **PostPlugin**. Хук `on_token` удалён 2026-10-12: Runtime
плагины голосуют по разобранным элементам встроенного StreamParser через
`on_upload(state)`; `should_stop` и `on_stream_end` убраны (семантика
останова — op-типы `on_upload`, финализация потока — работа PostPlugin).

| Секция | Когда | Частота | Назначение |
|--------|-------|---------|-----------|
| **Pre** | Перед каждой генерацией | N раз | Обогащение, фильтрация ввода; ленивая тяжёлая подготовка (1-й вызов, флаг в global) |
| **Runtime** | На каждый разобранный элемент стрима (WORD/SPECIAL) | На каждый элемент | Фильтры на лету, ранний стоп, guard-статистика |
| **Post** | После завершения генерации | N раз | Парсинг, валидация, retry, логирование |

### 4.1 Pre-плагины

Выполняются перед каждым вызовом LLM. Модифицируют messages перед отправкой.
Тяжёлая одноразовая подготовка (бывшая секция Init: загрузка BK-tree,
прекомпиляция промпта, health check GET /v1/models без генерации) —
лениво, при первом `on_input`, под защитным флагом в `self.global_memory`.

Типичные задачи:

- обогащение контекста (RAG + semantic + siblings) — конвейер подробно
  описан в docs/process.md §2;
- фильтрация PII во входе;
- обрезка длинной истории (для multi-turn агентов);
- подстановка переменных;
- rate limiting.

```python
class ContextEnricher(PrePlugin):        # ПЛАН — не реализован
    """CPU-обогащение через RAG по эмбеддингу brief'а. LLM НЕ решает что
    искать — поиск идёт по embedding(brief) до передачи контекста модели
    (process.md §2, зафиксировано 2026-10-10)."""

    def on_input(self, messages, context):
        task = context["task"]

        # Кэш в local памяти (для multi-turn); детерминирован: одинаковый
        # brief -> одинаковое обогащение
        cache_key = f"enrichment_{task.id}"
        cached = self.local_memory.get(cache_key)

        if not cached:
            qv = embed(task.brief)                   # models.embed — единый query-вектор
            docs = self.rag.search(qv, top_k=3)      # vector-first (BM25 — предфильтр)
            memories = self.semantic.recall(qv, top_k=3)
            siblings = self.kanban.get_completed_siblings(task.id)
            cached = self._format(docs, memories, siblings)
            self.local_memory.set(cache_key, cached)

        # Добавляем в последний user message — НЕ трогаем system!
        original = messages[-1]["content"]
        messages[-1]["content"] = f"{original}\n\nКонтекст:\n{cached}"
        return messages
```

Правила секции (бывшие правила Init перенесены сюда):

- видят весь список messages, но ИЗМЕНЯЮТ ТОЛЬКО user-сообщения —
  system/assistant возвращаются байт-в-байт (префиксный кэш llama.cpp, P8);
- тяжёлая подготовка — один раз под флагом global-памяти, не на каждый вызов.

### 4.2 Runtime-плагины

Работают на каждом **разобранном элементе потока** (WORD или SPECIAL из
встроенного StreamParser) во время стриминговой генерации. PARTIAL-элементы
плагинам не доставляются. Самые мощные и опасные: исполняются в горячем
цикле, стоимость одного плагина умножается на количество элементов (бюджет —
§9.2, запрет I/O — §12.2 п.3).

Типичные задачи:

- ранний стоп по маркерам (например, конец финального ответа);
- фильтрация PII на лету;
- guard-статистика и детекторы аномалий (зацикливание, таймауты);
- логирование в реальном времени.

Эталонный пример секции — стоп по маркеру конца декомпозиции:

```python
class StopOnDone(RuntimePlugin):         # ПЛАН — не реализован
    """Останавливает генерацию, когда встречен маркер конца ответа."""

    def on_upload(self, state):
        if state.status == ParseStatus.SPECIAL and state.word == "</done>":
            return {"op": "stop", "data": ""}
        return {"op": "continue"}
```

**API результата Runtime-плагина (`on_upload(state)`):**

```python
{"op": "continue"}                     # продолжать генерацию (дефолт базы)
{"op": "stop",  "data": ...}           # стоп БЕЗ ошибки (EOS, бюджет достигнут);
                                       # data может нести замену хвосту ответа
{"op": "error", "data": "..."}         # стоп С ошибкой (битый вывод, guard) —
                                       # агент поднимает её наружу
```

Правила секции:

- `on_upload` обязан отрабатывать за микросекунды (никакого I/O!);
- состояния на экземпляре плагина нет — временные буферы только в
  `self.local_memory` (агент трёт её каждым `run()`, reset() плагинам не нужен);
- **правило коммита**: элемент применяется к дереву сегментов (слово →
  `stack[-1].text`, special → открытие/закрытие Segment) только если ВСЕ
  Runtime-плагины вернули continue; первый не-continue прерывает голосование
  и останавливает поток (docs/stream_parser_model.md §7);
- порядок runtime-плагинов значим: голосование идёт списком, побеждает
  первое не-continue.

Известное расхождение поведения (открытый вопрос, фаза 2): str-LLM
(тестовый фейк) коммитит элементы без голосования, chunk-iterator —
строго через голосование; при подключении реального клиента унифицировать.

### 4.3 Post-плагины

Выполняются после завершения генерации. Постобработка финального ответа.
Финализация потока (`StreamParser.finalize`) к этому моменту уже выполнена
агентом: think/final разделены, ответ чистый — отдельного сплиттера нет
(OutputSplitterPlugin удалён 2026-10-12).

Типичные задачи:

- парсинг ответа в структуру (TaskParserPlugin — ЕДИНСТВЕННЫЙ реализованный
  плагин: protocol v2 блоки «Название / Описание», двухуровневый `<atom>`,
  F1–F3, дедупликация, запись дерева в project-ключ `tasks`);
- удаление дубликатов (входит в TaskParserPlugin, порог 0.75);
- проверка качества (например, вырожденный повтор родителя = атом);
- логирование в историю (JSONL + debug.log — ОБА блока, см. §2.4);
- retry при ошибках парсинга.

```python
class ResponseParser(PostPlugin):        # ПЛАН — его роль несёт TaskParserPlugin
    """Парсит текстовый ответ в список задач."""

    def __init__(self, parser_fn):
        self.parse = parser_fn

    def on_output(self, response, context):
        try:
            parsed = self.parse(response)  # TaskParserPlugin.parse_decomposition
            self.local_memory.set("parsed", parsed)
            return {"response": response, "parsed": parsed, "action": "accept"}
        except ParseError as e:
            self.local_memory.set("parse_error", str(e))
            return {"response": response, "action": "retry"}
```

**API результата Post-плагина:**

```python
{
    "response": str,       # может быть модифицирован
    "parsed": Any,         # опционально
    "metadata": dict,      # произвольные метаданные
    "action": "accept" | "retry" | "reject",
}
```

Важно: `retry` перезапускает ВЕСЬ цикл run() (pre → генерация → post),
а не только генерацию; число ретраев ограничено конфигом (`max_retries`,
по умолчанию 2), иначе — `AgentRejectedError`. В фактическом `Agent.run()`
(v0.1) не-accept просто прерывает цепочку post-плагинов и возвращается в
результате — цикл ретраев ещё не реализован (фаза 2).

---

## 5. Память — четыре уровня иерархии

### 5.1 Концепция уровней

Память — иерархическое хранилище данных, доступное всем плагинам
агента. Это единственный легальный канал коммуникации между плагинами
(§2.4) и между агентами (через project-уровень). Изоляция (§2.1 principles)
задаётся **уровнем**, а не запретом разделения: агент по умолчанию видит
только свои local/global; совместные данные явно кладутся на project или
workspace уровень.

**Эволюция модели (зафиксировать, чтобы не путаться):** в черновике это был
один объект `SessionMemory` с четырьмя скоупами внутри (set_local /
set_session / set_project / set_super). Фактическая реализация v0.1
(`memory.py`) — противоположная: ОДИН класс `Memory` = ОДНА плоская
KV-коробка, отвечающая только за себя; уровни — четыре отдельных
экземпляра, связанные ссылками вверх в иерархии объектов
Workspace → Project → Agent. Методы `set_*`/`get_*` c суффиксами уровня
в коде НЕ используются — вместо них `self.local_memory.set(...)` и т.п.

```
+-----------------------------------------------------------+
|                    WORKSPACE                               |
|      Общая для ВСЕХ агентов во ВСЕХ проектах               |
|      Персистентная (переживает перезапуски) — ПЛАН         |
|      Примеры: общая статистика, системные конфиги          |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                    PROJECT                                 |
|      Общая для всех агентов ВНУТРИ одного проекта          |
|      Персистентная (пока проект активен) — ПЛАН            |
|      Здесь живёт дерево задач: ключ "tasks"                |
|      Примеры: результаты исследований, общий контекст      |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                    AGENT GLOBAL                            |
|      Приватная для ОДНОГО агента (была "session")          |
|      Переживает итерации run(); персистентность — ПЛАН     |
|      Примеры: история диалога, статистика агента,          |
|               флаги ленивой тяжёлой подготовки             |
+-----------------------------+-----------------------------+
                              |
+-----------------------------v-----------------------------+
|                    LOCAL                                   |
|      Приватная для ОДНОГО вызова run()                     |
|      Сбрасывается в начале каждого run() (делает агент)    |
|      Примеры: parse_state, parse_errors, промежуточные     |
+-----------------------------------------------------------+
```

### 5.2 Матрица доступности

| Уровень | Видимость | Персистентность | Сброс |
|---------|-----------|-----------------|-------|
| **local** | Только этот агент в этом run | ❌ нет | Каждый `run()` (автоматически) |
| **global** | Только этот агент во всех run | 🟡 план (SQLite namespace) | При создании нового агента / очистке namespace |
| **project** | Все агенты в проекте | 🟡 план (БД проекта) | При закрытии проекта |
| **workspace** | Все агенты везде | 🟡 план (системная БД) | Никогда (или вручную) |

Персистентность верхних трёх уровней — план (sqlite за тем же API
`Memory`, см. docstring `memory.py`); в v0.1 все уровни in-memory.
Уровни будут реализованы через унифицированный бэкенд `PersistentStore`
(§6.5): global-level даст изоляцию агентов из §2.1, project-level —
контролируемый обмен (Producer-Consumer, §5.6).

### 5.3 API памяти

Фактический API (реализован, `memory.py`) — один класс на экземпляр
уровня; адресация уровня — переменной плагина, а не суффиксом метода:

```python
class Memory:                       # одна плоская KV-коробка
    def set(self, key: str, value: Any) -> None: ...
    def get(self, key: str, default: Any = None) -> Any: ...
    def clear(self) -> None: ...
    def subscribe(self, pattern: str, callback) -> None: ...   # glob-паттерн по ключам
    def unsubscribe(self, pattern: str, callback=None) -> None: ...
```

Владение уровнями (реализовано): `Workspace.memory`, `Project.memory`,
`Agent.global_memory` + `Agent.local_memory`; свойства `Agent.project_memory`
/ `Agent.workspace_memory` — ссылки вверх (могут быть None у невстроенного
агента). Плагин получает ВСЕ четыре экземпляра один раз в `bind()` как
атрибуты `workspace_memory / project_memory / global_memory / local_memory`.

Исторический черновик API (НЕ реализован, оставлен как ориентир для
персистентности): единый `SessionMemory` с методами `set_local/set_session/
set_project/set_super` (+ соответствующие get), `reset_local()`,
`reset_session()`, `subscribe(key, cb)`, `snapshot()`. Решение лида
2026-10-11 заменил его четырьмя отдельными `Memory`; имена `*_session`/
`*_super` выведены из обращения — в актуальной терминологии им соответствуют
`global` и `workspace`. `snapshot()` сохраняется как цель (для breakpoint'ов,
§5.8): снимок = `{level: dict-копия}` по четырём экземплярам.

### 5.4 Event-driven подписки

Плагины могут подписываться на изменения ключей (регистрация — при
добавлении плагина / первом on_input):

```python
# В плагине (после bind())
self.global_memory.subscribe("response_duration", self._on_duration)

# Когда где-то вызывается
memory.set("response_duration", {"duration_ms": 1500})

# Автоматически срабатывает
def _on_duration(self, key, value):
    # обновить статистику
    pass
```

Реализовано (`Memory.subscribe`): callback получает `(key, value)`;
паттерн — glob по имени ключа (`fnmatch`, например `subscribe("tasks:*", cb)`),
отписка есть (`unsubscribe`). Подписчики вызываются синхронно; исключение в
подписчике логируется и НЕ прерывает set(). Черновое правило
«callback получает (key, value, level)» снято: уровень задаётся тем, на
каком экземпляре Memory подписались, третий аргумент не нужен.

**Решение (зафиксировано):** скоуп подписки остаётся строго per-level —
вариант А. Convenience-обёрток вроде `Agent.subscribe_all(pattern, cb)`
(подписка сразу на все четыре уровня) и propagation событий от низших
уровней к высшим НЕ вводим. Если плагину нужно отслеживать ключ, который
может появиться на любом уровне, он явно подписывается на каждый нужный
экземпляр Memory (`local_memory.subscribe(...)`, `project_memory.subscribe(...)`
и т.д.). Обоснование: явность дешевле магии — propagation сделал бы
невозможным предсказание, сколько раз вызовется callback, и усложнил бы
отладку; а `subscribe_all` скрывал бы привязку плагина к уровню, которая
является осмысленной частью его контракта.

### 5.5 Жизненный цикл и создание памяти (реализовано)

Память НЕ создаётся отдельной фабрикой — она является частью объектов
иерархии (`memory.py`) и конструируется вместе с ними; плагин получает
все четыре экземпляра один раз в `bind()` (вызывается из
`Agent.add_plugin()`). Порядок создания — сверху вниз, связи вверх
появляются при встраивании:

```python
# При старте системы
workspace = Workspace(name="default")                 # workspace.memory
project = Project(name="blog-api-project")            # project.memory
workspace.add_project(project)                        # project.workspace = workspace

# Для каждого агента в проекте
decomposer = Agent(name="decomposer", llm=llm)        # global_memory + local_memory
executor = Agent(name="executor", llm=llm)
project.add_agent(decomposer)                         # decomposer.project = project
project.add_agent(executor)

# decomposer и executor делят project-память (один объект),
# но их global/local хранилища изолированы
```

Создание агента вне проекта допустимо: тогда `project_memory` и
`workspace_memory` плагина равны `None` — код плагина обязан это
учитывать (или агент собирается только встроенным; строгое требование к
встроенности — план, см. открытый вопрос §5.9).

Видимость между агентами (фактический API — адресация экземпляром,
не суффиксом метода):

```python
# Decomposer Agent пишет на общий уровень
decomposer.project_memory.set("research_jwt_findings", {...})

# Executor Agent может прочитать (тот же объект Memory)
jwt_info = executor.project_memory.get("research_jwt_findings")

# Но НЕ видит global-счётчик decomposer'а — это СВОЙ счётчик executor'а:
calls = executor.global_memory.get("calls_count")   # → None
```

### 5.6 Паттерны использования

Все примеры — фактический API: адресация нужного уровня атрибутом плагина
(`self.local_memory` / `self.global_memory` / `self.project_memory` /
`self.workspace_memory`, bound в `bind()`).

**Producer-Consumer внутри одного run() (Runtime → Post):**

```python
# Runtime-плагин пишет в local (например, собирает think-сегменты)
self.local_memory.set("think_content", state.word)

# Post-плагин читает
think = self.local_memory.get("think_content", "")
```

На практике этот канал сейчас использует сам агент: перед POST-фазой он
кладёт в local `parse_state` (живое состояние StreamParser), а в
`context` — собранный `think` из THINK-сегментов дерева.

**Producer-Consumer между агентами (Researcher → Executor):**

```python
# Researcher Agent (producer) — результат исследования в project-память
self.project_memory.set("research_stripe_api", {
    "version": "2025-10",
    "auth": "Bearer token",
    "endpoints": {...},
})

# Executor Agent (consumer) — тот же объект Memory через свой project
stripe_info = self.project_memory.get("research_stripe_api")
```

**Счётчики на global-уровне (read-modify-write через get/set, переживает run()):**

```python
self.global_memory.set("calls_count", self.global_memory.get("calls_count", 0) + 1)
self.global_memory.set("tokens_used", self.global_memory.get("tokens_used", 0) + tokens)

avg = self.global_memory.get("tokens_used") / self.global_memory.get("calls_count")
```

**Сквозная статистика на workspace-уровне (редко!; может быть None у
невстроенного агента — проверять):**

```python
# В post-плагине любого агента
if self.workspace_memory is not None:
    s = self.workspace_memory.get(
        "global_stats",
        {"total_calls": 0, "total_duration_ms": 0, "total_tokens": 0},
    )
    self.workspace_memory.set("global_stats", {
        "total_calls": s["total_calls"] + 1,
        "total_duration_ms": s["total_duration_ms"] + duration,
        "total_tokens": s["total_tokens"] + tokens,
    })
```

**Кэш в рамках run() (local):**

```python
cached = self.local_memory.get(f"enrichment_{task.id}")
if not cached:
    cached = expensive_computation()
    self.local_memory.set(f"enrichment_{task.id}", cached)
```

**Флаги координации (local, читает оркестратор после run — §11.3):**

```python
# Плагин A
self.local_memory.set("needs_research", True)
self.local_memory.set("research_query", "как работает X")

# Плагин B (позже в цепочке post)
if self.local_memory.get("needs_research"):
    query = self.local_memory.get("research_query")
    # запустить исследование
```

### 5.7 Соглашения об именовании ключей

| Префикс/ключ | Уровень | Пример | Кто пишет |
|---------------|---------|--------|-----------|
| `parse_state` | local | живое состояние StreamParser | агент (перед POST) |
| `think_*` | local | `think_content`, `think_length` | Runtime |
| `enrichment_*` | local | `enrichment_0.2.1` | Pre |
| `parsed`, `parse_error` | local | результат парсинга | Post |
| `needs_*`, флаги | local | `needs_research` | любой |
| `*_stats`, `calls_count` | global | `think_stats`, `conversation_history` | StatisticsCollector |
| `rate_limit_calls` | global | счётчики лимитов агента | Pre/Post |
| `research_*`, `task_*_result` | project | findings, результаты задач | Post/оркестратор |
| `tasks` | project | иерархия задач `{id: Task}` после декомпозиции (process §1.3, data.md §1) | Post (TaskParserPlugin) |
| `global_stats`, `system_version` | workspace | сквозная статистика | MetricsWriter |

Ключи без префикса — локальные переменные одного плагина, другим секциям
не гарантируются. Правило выбора уровня: по умолчанию пишется на самый
приватный подходящий уровень; повышение уровня — осознанное решение
(лидер проекта: workspace-уровень использовать только для статистики и
системных конфигов).

### 5.8 Breakpoints и recovery (план — НЕ реализовано)

Пересмотр черновика roadmap §3.3: отдельные `_stats`/`_breakpoints` поля
**не вводятся** — их роль полностью берут на себя global/project уровни
(когда станут персистентными, §5.2). Остаются только точки
восстановления; черновик API ниже — цель, а не факт (реализация отложена
до появления персистентности, фаза 2+ плана):

```python
class BreakpointManager:
    """Сохраняет снапшоты состояния для восстановления.

    PLAN: memory-аргумент — не единый SessionMemory (его больше нет,
    §5.3), а ссылка на Agent: snapshot() = {level: dict-копия} по всем
    четырём экземплярам памяти агента.
    """

    def __init__(self, agent: "Agent", storage_path: Path): ...

    def save_breakpoint(self, label: str, context: dict = None):
        """append JSONL-записи {label, timestamp, context, snapshot()}"""

    def load_breakpoint(self, label: str) -> dict | None:
        """последняя точка с таким label"""

    def restore_from(self, breakpoint: dict):
        """local пересоздаётся из снапшота; global восстанавливается
        целиком; project/workspace НЕ трогаем — они персистентные и могли
        измениться другими агентами пока процесс был мёртв"""
```

Правила (переформулированы под v3-терминологию):

- Сброса «session» больше нет: `Agent.run()` сам вызывает
  `local_memory.clear()` в начале каждого прогона; `global_memory`
  чистится только при создании нового агента (или cleanup-плагином).
- Breakpoint'ы пишутся в `data/breakpoints/{agent_name}.jsonl` плагином
  `AutoCheckpoint` (Post) после каждого успешного run и при FAILED
  задачах; ротация — хранить 20 последних (gzip; открытый вопрос
  roadmap §9.1 решён в пользу JSONL+gzip).
- Подписки (§5.4) работают на всех четырёх уровнях; callback получает
  `(key, value)` — уровень задаётся экземпляром, на котором подписались.
- Механика `cascagent resume` (kanban как источник истины о задачах) —
  `docs/roadmap.md` §3.3; память дополняет её состоянием агента.
- Логику MemoryEntry/актуальности semantic memory (#8) см. roadmap §4.4 —
  это отдельный слой `semantic.py` (не реализован), а не `Memory`.

### 5.9 Гарантии и ограничения

**Гарантии (фактические, v0.1):**

1. Последовательность: всё однопоточно, никаких race conditions.
2. Изоляция local: очищается агентом в начале каждого run(); не видна
   другим run, другим агентам, другим процессам.
3. Отсутствие коллизий: одинаковые ключи на разных уровнях — разные
   данные (разные объекты Memory).
4. Исключение в подписчике логируется и не прерывает set().

**План (приход sqlite за тем же API `Memory`, docstring `memory.py`):**

1. Атомарность записи: каждая операция set завершается полностью
   или не выполняется (SQLite-транзакция на запись).
2. Персистентность: global/project/workspace переживают перезапуск
   процесса.

**Ограничения (принятые сознательно):**

1. Нет распределённых транзакций: запись в project и workspace — отдельные
   операции.
2. Нет блокировок: не нужны (нет параллелизма, MVP однопоточный).
3. Нет TTL: очистка только через cleanup-плагины (`SessionCleanup`, §7.1).
4. Нет версионирования записей: если надо — делает плагин (например,
   version-поле в значении).
5. Подписка привязана к экземпляру: callback вызывается только на
   изменения того Memory, на котором подписались (третий аргумент
   «уровень» не нужен, §5.4).
6. Открытый вопрос (перенесён из снятых §9.1–§9.3): должен ли `Agent`
   требовать обязательной встроенности в `Project` (чтобы
   `project_memory` никогда не был None и плагины не проверяли None)?
   Пока — допустим None, ответственность на плагине.

---

## 6. Базовые классы

Фактическая реализация живёт в двух модулях: `src/cascagent/memory.py`
(`Memory`, `Workspace`, `Project`, `Agent`) и
`src/cascagent/plugins/base.py` (`Plugin`, `PrePlugin`, `RuntimePlugin`,
`PostPlugin`). Раздел 6.1 показывает код как есть (v0.1); 6.2 — объектную
иерархию уровней; 6.3 — классы плагинов; 6.4 — оркестратор `Agent.run()`;
6.5–6.7 — **план** (персистентность, фабрика, breakpoints; к v0.1 не
реализованы, оставлены как ориентир проектирования).

### 6.1 Memory — плоская KV-коробка (реализовано)

Один класс = один уровень = один dict. Никаких скоупов внутри; четыре
уровня — четыре экземпляра (§5.1).

```python
class Memory:
    """A flat KV store with glob-pattern subscriptions (memory.py)."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._data: Dict[str, Any] = {}
        self._subscribers: List[tuple] = []      # [(glob pattern, callback)]

    # -- CRUD ---------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._notify(key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)      # read callbacks НЕ вызывает

    def clear(self) -> None:
        """Bulk wipe без per-key событий (вызывает Agent.run() на local)."""
        self._data.clear()

    # -- observation (glob subscriptions) ------------------------------
    def subscribe(self, pattern: str,
                  callback: Callable[[str, Any], None]) -> None:
        """fnmatch-glob по именам ключей: "tasks:*", "*.error", "*" — всё."""
        self._subscribers.append((pattern, callback))

    def unsubscribe(self, pattern: str,
                    callback: Optional[Callable] = None) -> int:
        before = len(self._subscribers)
        self._subscribers = [
            (p, cb) for p, cb in self._subscribers
            if not (p == pattern and (callback is None or cb is callback))
        ]
        return before - len(self._subscribers)

    def _notify(self, key: str, value: Any) -> None:
        for pattern, callback in self._subscribers:
            if fnmatch.fnmatchcase(key, pattern):
                callback(key, value)             # исключение — логируется, set() не прерывать
```

Отличия от чёрного черновика `SessionMemory` (снято, см. §5.3): нет
суффиксов `_local/_session/_project/_super`, нет `reset_local()/
reset_session()` (очистку local делает агент через `clear()`), нет
`snapshot()` (цель для breakpoint'ов, §5.8), подписка — glob-паттерн, а
не точный ключ, callback `(key, value)` без третьего аргумента.

### 6.2 Иерархия Workspace / Project / Agent (реализовано)

```python
class Workspace:
    def __init__(self, name="workspace", **config):
        self.projects: Dict[str, Project] = {}
        self.memory = Memory(name=f"{name}.workspace")
    def add_project(self, project) -> Project:   # project.workspace = self

class Project:
    def __init__(self, name, workspace=None, **config):
        self.agents: Dict[str, Agent] = {}
        self.workspace = workspace
        self.memory = Memory(name=f"{name}.project")
    @property
    def workspace_memory(self): ...              # ссылка вверх или None
    def add_agent(self, agent) -> Agent:         # agent.project = self

class Agent:
    def __init__(self, name, project=None, llm=None,
                 pre_plugins=(), runtime_plugins=(), post_plugins=(),
                 parser_config=None):
        self.global_memory = Memory(name=f"{name}.global")
        self.local_memory  = Memory(name=f"{name}.local")
        ...                                      # add_plugin() биндит все 4 уровня
    @property
    def project_memory(self): ...                # self.project.memory или None
    @property
    def workspace_memory(self): ...              # проект → workspace или None
```

Черновик `AgentSession` (dataclass с `cache_key/indices/precompiled_prompt`,
заполняемым Init-плагинами) **снят**: Init-секции больше нет, ленивая
тяжёлая подготовка хранит свои артефакты в `global_memory` под ключами
domain-плагинов (например `indices`, `precompiled_prompt`) — отдельный
объект состояния не нужен. Поле `tools` появится вместе с MCP-слоем
(roadmap §4, не реализовано).

### 6.3 Базовые классы плагинов (реализовано)

> **v3 (2026-10-11, решения лида).** Секции переименованы: InitPlugin убран
> (его роль — ленивая тяжёлая подготовка в PrePlugin), InputPlugin → PrePlugin,
> TriggerPlugin → RuntimePlugin, OutputPlugin → PostPlugin. Из API убраны
> `reset()` (очистка — ответственность агента: `run()` стирает local-скоуп
> памяти), `should_stop` и `on_stream_end` (семантика останова живёт в op-типах
> `on_upload(state)`; с 2026-10-12 хук `on_token` удалён — плагины голосуют по
> разобранным элементам встроенного StreamParser; финализация потока — работа
> парсера/агента). Память биндится в плагин
> ОДИН РАЗ (`bind()`), а не прокидывается аргументом в каждый хук — хот-путь
> на элемент потока не тащит лишние параметры. Плагин видит ВСЕ четыре уровня:
> `self.workspace_memory / project_memory / global_memory / local_memory`
> (верхние могут быть None у невстроенного агента, §5.5).

```python
class Plugin(ABC):                     # общий предок: name/config/bind()
    name = "base"                      # ключ PLUGIN_REGISTRY (§12.1)

    def __init__(self, **config):      # self.config — TOML-параметры как есть
        ...                            # + 4 атрибута *_memory = None до bind()

    def bind(self, workspace_memory=None, project_memory=None,
             global_memory=None, local_memory=None) -> "Plugin":
        """Wire ALL hierarchy memories (called once by Agent.add_plugin)."""

class PrePlugin(Plugin):               # секция PRE (был InputPlugin)
    name = "base_pre"

    @abstractmethod
    def on_input(self, messages: list, context: dict) -> list:
        """Меняет ТОЛЬКО user-сообщения; system/assistant — байт-в-байт
        (префиксный кэш llama.cpp, P8)."""

class RuntimePlugin(Plugin):           # секция RUNTIME (был TriggerPlugin)
    name = "base_runtime"

    def on_upload(self, state) -> dict:
        """Голосование по готовому элементу потока (WORD/SPECIAL) от
        встроенного StreamParser; state — живой ParseState
        (state.status, state.word).
        {"op": "continue"} | {"op": "stop", "data": ...}
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

Единственная конкретная реализация v0.1 — `TaskParserPlugin`
(`plugins/task_parser.py`, PostPlugin); реестра `PLUGIN_REGISTRY` и
загрузчика entry points пока нет (§12.1 — план).

### 6.4 Agent (оркестратор, реализовано)

Фактический цикл `run()` (memory.py) — без `AgentResult`/`bind(memory)` из
черновика: результат это dict, память принадлежит самому агенту.

```python
class Agent:
    def run(self, messages: list, context: dict | None = None) -> dict:
        # 1. local_memory.clear()                    # изоляция прогона (§5.6)
        # 2. PRE:    messages = p.on_input(messages, context)   # цепочкой
        # 3. LLM+RUNTIME:
        #    parser = self._make_parser()            # свежий StreamParser
        #    local_memory["parse_state"] = parser.state
        #    for chunk in gen:                       # стрим или str-файк
        #        for ev in parser.feed(chunk):       # WORD/SPECIAL элементы
        #            голосуют ВСЕ runtime-плагины on_upload(state);
        #            continue у всех -> parser.commit(ev),
        #            иначе stop-голос {op, **res} -> элемент НЕ коммитится
        #    parser.finalize("stop"|"error")         # хвост буфера флашится
        #    op == "error" -> RuntimeError
        # 4. context["response"], context["think"]   # think из THINK-сегментов
        # 5. POST:   r = p.on_output(response, context)  # цепочкой
        #    result = {"response", "context", "segments", "finish_reason",
        #              "parsed", "metadata", "action"}
        #    action != "accept" -> выход из цепочки   # retry/reject — план:
        #    # max_retries/AgentRejectedError в код ещё не завезены (§9.3)
        return result
```

Отличия от черновика §6.4 (снято): нет `init_plugins/input_plugins/
trigger_plugins/output_plugins` (секции pre/runtime/post), нет отдельного
`bind(memory)` (память создаётся в `__init__`, биндинг плагинов делает
`add_plugin()`), нет `_generate_with_triggers` с `should_stop/on_token/
on_stream_end` (голосование по элементам `on_upload`), нет `tools` и
`system_prompt` параметром (промпт приносит вызывающий в `messages`;
`SYSTEM_PROMPT` — константа модуля task_parser). Поведение str-LLM:
элементы коммитятся без голоса плагинов (файк не стрим) — расхождение с
chunk-iterator зафиксировано как открытый вопрос (roadmap фаза 2).

### 6.5 MemoryStore — унифицированный бэкенд (ПЛАН, не реализовано)

Все персистентные уровни памяти планируется строить на одном абстрактном
KV-хранилище (sqlite за тем же API, docstring `memory.py`). К v0.1 ни один
класс ниже не существует в коде — раздел описывает целевой дизайн:

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

### 6.6 MemoryFactory (ПЛАН, не реализовано)

Фабрика была нужна чёрной модели `SessionMemory` (три store'а на один
объект). В v3 память создаётся объектами иерархии (§5.5), поэтому от
фабрики остаётся только задача «подключить sqlite-бэкенды к нужным
namespace'ам» — она ляжет на конструкторы `Workspace/Project/Agent`,
когда придёт персистентность. Черновик namespace'ов (термины session →
global, super → workspace):

| Уровень | БД | namespace |
|---------|----|-----------|
| global | projects.db | `agent:{agent_name}` (или session-id — решится при реализации) |
| project | projects.db | `project:{project_id}` |
| workspace | super_global.db | `system` |

### 6.7 BreakpointManager (ПЛАН, не реализовано)

Черновик ниже написан под снятый `SessionMemory`; при реализации
(`breakpoints.py` нет в дереве) конструктор принимает `Agent`, а
`snapshot()` строится копированием четырёх экземпляров `Memory`
(§5.8; контракт формы снимка `{level: dict-копия}` зафиксирован в §11.4.1). Логику save/load/restore оставляем как ориентир формата JSONL;
метод `restore_from()` переписан под фактический API v0.1
(`Memory.set/get/clear`, без суффиксов-скоупов):

```python
class BreakpointManager:
    """Точки восстановления (§5.8). Формат — JSONL, append-only."""

    def __init__(self, agent: "Agent", storage_path: Path):
        # ПЛАН: память агента — четыре экземпляра Memory (§5.3),
        # а не единый SessionMemory из черновика.
        self.agent = agent
        self.storage = storage_path

    def save_breakpoint(self, label: str, context: dict | None = None):
        # ПЛАН: snapshot() = {level: dict-копия} по четырём Memory (§5.8);
        # метода Agent.snapshot() в v0.1 ещё нет.
        snap = {
            "local":     dict(self.agent.local_memory._data),
            "global":    dict(self.agent.global_memory._data),
            "project":   dict(self.agent.project_memory._data)
                         if self.agent.project_memory else {},
            "workspace": dict(self.agent.workspace_memory._data)
                         if self.agent.workspace_memory else {},
        }
        bp = {
            "label": label,
            "timestamp": datetime.now().isoformat(),
            "context": context or {},
            "snapshot": snap,
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
        """local пересоздаётся; global (был \"session\") восстанавливается
        целиком; project/workspace НЕ трогаем (персистентны, могли измениться
        другими агентами пока процесс был мёртв)."""
        snap = breakpoint["snapshot"]
        local = self.agent.local_memory
        local.clear()                        # bulk wipe без per-key событий
        for k, v in snap.get("local", {}).items():
            local.set(k, v)
        gmem = self.agent.global_memory
        gmem.clear()
        for k, v in snap.get("global", {}).items():
            gmem.set(k, v)
```

---

## 7. Каталог встроенных плагинов (ПЛАН)

**Ниже — целевой каталог, а не факт v0.1.** Реализован ровно один плагин:
`TaskParserPlugin` (PostPlugin, `plugins/task_parser.py`) — парсинг
декомпозиции + F1–F3 + подстановка дерева в project-ключ `tasks`.
Названия секций в таблицах — старые (Init/Input/Trigger/Output);
соответствие v3: Init → Pre (ленивая подготовка по флагу в global),
Input → Pre, Trigger → Runtime (`on_upload`), Output → Post. Все
остальные строки — backlog аудита (привязка «пробел → плагин»:
docs/roadmap.md §7).

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

## 8. Конфигурации агентов (ПЛАН)

Конфигурационный слой не реализован: `docs/config.md` — тоже черновик,
TOML-загрузчика и реестра в коде нет. Агенты v0.1 собираются кодом
(примеры §11). Названия ключей ниже — старые (`init/input/trigger/output`);
при реализации соответствуют секциям v3 `pre/runtime/post` (Init-ключа не
будет — его роль у ленивой подготовки в Pre, §4.2). Порядок плагинов в
списке = порядок исполнения.

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
| Изоляция вызова | local.clear() в run() | то же | то же | то же |

### 8.6 Конфигурация памяти (ПЛАН)

Общая для всех агентов проекта (в `config.toml`, см. `docs/config.md`;
к v0.1 ничего не читается — память полностью in-process, §5.2):

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

Эти значения пойдут в конструкторы иерархии (`Workspace/Project/Agent`),
когда придёт sqlite-персистентность (§6.5–6.6 — план); session/agent-id
попадёт в breakpoint-файл и trace (#4, #6).

---

## 9. Жизненный цикл

### 9.1 Последовательность фаз одного run() (реализовано)

```
run(messages, context)
  |
  +-- local_memory.clear()            # local чист; global/project/workspace живут
  +-- PRE plugins   (цепочкой: messages = p.on_input(messages, context))
  |     читают project/global/workspace, пишут local (промежуточное)
  +-- GENERATION + RUNTIME (стрим chunk'ами через встроенный StreamParser)
  |     для каждого ГОТОВОГО элемента (WORD/SPECIAL):
  |       голосуют все runtime-плагины on_upload(state)
  |       continue у всех -> parser.commit(ev); иначе {op,...} -> элемент не коммитится
  |     конец стрима: parser.finalize("stop"|"error") -> флаш хвоста буфера
  |     op == "error" -> RuntimeError(data)
  +-- context["response"], context["think"]   # think = THINK-сегменты дерева
  +-- POST plugins  (парсинг TaskParserPlugin -> ...)
  |     читают local/context; пишут parsed/metadata/action в результат;
  |     могут писать global (статистика), project (tasks), workspace (суммы)
  |     action != "accept" -> выход из цепочки
  |     # retry/reject-цикл (max_retries, AgentRejectedError) — ПЛАН, в коде нет
  +-- return {"response", "context", "segments", "finish_reason",
              "parsed", "metadata", "action"}
      # local будет очищен в начале следующего run
```

Тайминг-оверхед памяти: все четыре уровня — dict (наносекунды);
SQLite-запись (<1 мс на ключ) появится с персистентностью (§5.2 — план).
Горячий путь runtime-плагинов использует только живые атрибуты state/local
(§12.2 п.3).

### 9.2 Тайминги и частоты

| Фаза | Частота | Бюджет времени (референс GTX 1650 Ti) |
|------|---------|----------------------------------------|
| add_plugin()/bind() | 1 раз при сборке агента | ~0 (dict-биндинг); загрузка индексов — план, 0.1–2 c |
| pre-плагины | перед каждым вызовом LLM | RAG/semantic: <50 мс; Selective Context: <200 мс (план) |
| runtime на элемент | каждый разобранный элемент потока | суммарно все плагины < 0.1 мс/элемент |
| post-плагины | после генерации | парсинг+детекторы < 20 мс; запись истории < 5 мс |
| retry | по необходимости | полный повтор цикла (дорого! лимит 2) — план |

Генерация остаётся доминирующей статьёй затрат (30–55 tok/s); плагины не
должны замедлять её заметнее, чем на 5% (проверяется `scripts/benchmark.py` —
скрипт не реализован, roadmap фаза 2).

### 9.3 Обработка ошибок

| Источник ошибки | Поведение |
|-----------------|-----------|
| Pre-плагин упал | **факт v0.1: исключение летит из run()** (fail fast); «логируется и пропускается» — план |
| Runtime-плагин вернул stop | поток останавливается без ошибки, finish_reason="stop" |
| Runtime-плагин вернул error | finalize("error") + RuntimeError(data) |
| Runtime-плагин упал | **факт v0.1: исключение летит из run()**; «отключается на текущий стрим» — план |
| Post-плагин вернул retry/reject | **факт v0.1: только выход из цепочки post**; повтор run()/AgentRejectedError — план |
| LLM недоступна | клиент ретраит с backoff; после лимита — TaskFailedError в оркестратор (план) |

Принцип (целевой): отказ вспомогательного плагина НЕ должен срывать основной
вызов LLM; отказ критического (TaskParserPlugin) — обязан быть виден, а не
проглочен. Пока изоляции try/except в цикле агента нет — все исключения
прокидываются вызывающему (зафиксировано в §4.1).

---

## 10. Структура проекта (реализовано)

Фактическое дерево плагинного слоя (ветка `plugin`):

```
src/cascagent/
├── memory.py                      # Memory, Workspace, Project, Agent (§6.1–6.2, §6.4)
├── stream_parser.py               # встроенный сервис потока (не плагин;
│                                  # docs/stream_parser_model.md)
├── models.py                      # Task/SnowflakeID — данные (docs/data.md)
├── plugins/
│   ├── __init__.py                # ре-экспорт базовых классов + TaskParserPlugin
│   │                              # (PLUGIN_REGISTRY — план, §12.1)
│   ├── base.py                    # Plugin/PrePlugin/RuntimePlugin/PostPlugin (§6.3)
│   └── task_parser.py             # TaskParserPlugin: SYSTEM_PROMPT, parse_decomposition,
│                                  # similarity/is_parent_repeat/is_duplicate, on_output (§7.4)
tests/
├── test_models.py                 # Task/SnowflakeID (docs/data.md)
├── test_bk_tree.py                # BK-tree (docs/algorithms.md)
├── test_plugins_base.py           # базовые классы, bind(), иерархия Workspace/Project/Agent
├── test_plugins_task_parser.py    # парсинг+F1–F3+подстановка в project tasks, snapshot промпта
└── conftest.py                    # фикстуры (в корне проекта)
```

План (в дереве отсутствует, см. §6.5–6.7, §7): `breakpoints.py`, подкаталоги
`plugins/init|input|trigger|output/` со списанными плагинами,
`test_stream_parser.py`/`test_memory.py` (Memory и StreamParser пока
покрываются тестами базовых классов и task_parser), `test_plugins_*.py` по
файлу на секцию. Модули-предшественники `parser.py` и `detector.py` **удалены**
— их логика поглощена `TaskParserPlugin`; `enricher.py` в текущем дереве
отсутствует (RAG — plan, roadmap фаза 2).

---

## 11. Примеры использования

### 11.1 Hello World через Agent API (реализовано)

```python
from cascagent.memory import Project, Agent
from cascagent.models import Task
from cascagent.plugins.task_parser import SYSTEM_PROMPT, TaskParserPlugin

project = Project(name="blog-api")
agent = Agent(
    name="decomposer",
    project=project,                          # даёт плагину project_memory
    llm=client,                               # callable(messages)->str|chunk-iter
    post_plugins=[TaskParserPlugin(threshold=0.75)],
)
project.add_agent(agent)

messages = [
    {"role": "system", "content": SYSTEM_PROMPT},   # статический префикс (P8)
    {"role": "user", "content": f"Задача: {task.brief}"},
]
result = agent.run(messages, context={"task": task})

if result["parsed"] == "<atom>":
    print("Задача атомарна -> Executor")
else:
    for sub in result["parsed"]:
        print(f"- {sub.brief}")
```

Think и состояние потока — в результате и local памяти (§2.4):

```python
think = result["context"]["think"]              # THINK-сегменты дерева
segments = result["segments"]                   # всё дерево сегментов
state = agent.local_memory.get("parse_state")   # живой ParseState (до след. run)
```

### 11.2 Свой pre-плагин: подстановка имени проекта

```python
from cascagent.plugins.base import PrePlugin

class ProjectVars(PrePlugin):
    name = "project_vars"

    def __init__(self, variables: dict[str, str]):
        super().__init__()
        self.variables = variables

    def on_input(self, messages, context):      # память — self.*_memory (bind)
        messages[-1]["content"] = messages[-1]["content"].format(**self.variables)
        return messages
```

### 11.3 Producer-Consumer: координация Research через память

```python
from cascagent.plugins.base import PostPlugin

class ResearchNeededDetector(PostPlugin):
    """CPU-эвристика: пустой/нерелевантный decompose-ответ -> флаг исследования."""
    name = "research_needed"

    def on_output(self, response, context):
        if not context["task"].parent_result and len(response) < 40:
            self.local_memory.set("needs_research", True)
            self.local_memory.set("research_query", context["task"].brief)
        return {"response": response, "action": "accept"}

# Оркестратор ПОСЛЕ run() читает флаги (не агенты между собой!):
result = decomposer.run(messages, context={"task": task})
if decomposer.local_memory.get("needs_research"):
    finding = researcher.run([...])       # local переживает run до следующего запуска
```

### 11.4 Статистика через подписки

```python
class ThinkStats(PostPlugin):
    name = "think_stats"

    def bind(self, **memories):                 # вызывается add_plugin()
        super().bind(**memories)
        self.global_memory.subscribe("response_duration", self._on_done)
        self.global_memory.set("think_stats", {"calls": 0, "total_ms": 0, "max_ms": 0})

    def _on_done(self, key, value):             # callback (key, value) — без scope
        s = self.global_memory.get("think_stats")
        self.global_memory.set("think_stats", {
            "calls": s["calls"] + 1,
            "total_ms": s["total_ms"] + value["duration_ms"],
            "max_ms": max(s["max_ms"], value["duration_ms"]),
        })

    def on_output(self, response, context):
        self.global_memory.set("response_duration", {
            "duration_ms": context.get("duration_ms", 0),
            "task_id": context["task"].id,
        })
        return {"response": response, "action": "accept"}

# после серии run() статистика читается с global-уровня:
stats = agent.global_memory.get("think_stats")  # {"calls": 12, ...}
dump = dict(agent.global_memory._data)          # отладочный дамп (snapshot() — план, §5.8)
```

Подписка glob'ом по семейству ключей — так же, другим паттерном:
`self.project_memory.subscribe("tasks:*", cb)`. Скоуп строго per-level
(решение зафиксировано, §5.4): если ключ может появиться на нескольких
уровнях, подписываемся явно на каждый из них; обёрток и propagation нет.
Простейшая альтернатива без подписки — прямо в post-плагине:

```python
self.global_memory.set("calls_count", self.global_memory.get("calls_count", 0) + 1)
```

### 11.4.1 Снимок памяти (snapshot) — ПЛАН

Метода `Agent.snapshot()` в v0.1 **нет** (проверено по дереву: ни в
`memory.py`, ни в плагинах). Для отладочного дампа сейчас допустим прямой
доступ к `_data`, но это внутренний атрибут — не API. Целевая форма
(реализация вместе с breakpoint'ами, §6.7):

```python
def snapshot(self) -> Dict[str, Dict[str, Any]]:
    """{level: dict-копия} по четырём экземплярам Memory агента."""
    return {
        "local":     dict(self.local_memory._data),
        "global":    dict(self.global_memory._data),
        "project":   dict(self.project_memory._data) if self.project_memory else {},
        "workspace": dict(self.workspace_memory._data) if self.workspace_memory else {},
    }
```

Семантика снимка согласована с формой JSONL-записи BreakpointManager
(§6.7); формат `{level: {...}}` фиксируем как контракт ещё до реализации,
чтобы тесты breakpoint'ов писались против него сразу.

### 11.5 Полная сборка Decomposer-проекта (code, не TOML)

```python
def make_decompose_project(client) -> Project:
    project = Project(name="blog-api")
    agent = Agent(
        name="decomposer",
        project=project,
        llm=client,
        parser_config={"max_depth": 2, "fuzzy_max_distance": 1},  # [parser] — plan
        post_plugins=[TaskParserPlugin(threshold=0.75)],
    )
    project.add_agent(agent)
    return project
```

---

## 12. Расширение системы

### 12.1 Реестр плагинов и entry points (ПЛАН)

Реестра в коде нет: `plugins/__init__.py` только ре-экспортирует базовые
классы и `TaskParserPlugin`. Целевой дизайн — конфиг ссылается на плагин
по имени строки:

```python
# src/cascagent/plugins/__init__.py  (PLAN)
PLUGIN_REGISTRY: dict[str, type] = {
    "TaskParserPlugin": TaskParserPlugin,   # единственный встроенный пока
    # "IndexLoader", "ContextEnricher", ... — по мере реализации §7
}

def build_plugin(spec: dict) -> object:
    """spec = {"plugin": "ContextEnricher", "top_k": 3, ...}"""
    cls = PLUGIN_REGISTRY[spec["plugin"]]
    kwargs = {k: v for k, v in spec.items() if k != "plugin"}
    return cls(**kwargs)
```

Внешние пакеты смогут добавлять плагины через entry point группы
`cascagent.plugins` (pyproject.toml — секции entry-points там пока нет):

```toml
[project.entry-points."cascagent.plugins"]
my_limiter = "mypackage.plugins:TokenBudgetLimiter"
```

Загрузчик допишет PLUGIN_REGISTRY найденными entry points при старте CLI.

### 12.2 Правила расширения (обязательны к соблюдению)

1. **Плагин не имеет скрытого состояния между run()** — всё состояние только
   в памяти четырёх уровней (иначе ломается воспроизводимость §2.1).
2. **Pre-плагины не трогают system-сообщение** — статический префикс нужен
   для префиксного кэша llama.cpp; динамика живёт в user.
3. **Runtime-плагины без I/O и аллокаций в горячем пути** — никаких сетевых
   вызовов, записей на диск, эмбеддингов на элемент потока.
4. **Post-плагины идемпотентны** — повторный прогон на том же ответе даёт
   тот же результат (retry полагается на это; сам retry-цикл — план §9.3).
5. **Никаких обращений к другим агентам напрямую** — координация только через
   оркестратор и флаги local-памяти (§11.3) или project-уровень
   (Producer-Consumer, §5.6).
6. **Данные кладутся на самый приватный подходящий уровень** (§5.7): local →
   global → project → workspace; запись в project/workspace из pre/runtime
   запрещена (только post и ленивая подготовка в pre по флагу).
7. **Без эвристик атомарности** — `is_atom` решает модель (`<atom>`),
   QualityValidator проверяет лишь формат (ADR-007). Единственные
   детерминированные CPU-исключения — правила F1–F3 плагина TaskParserPlugin
   (process §1.3: degenerate-repeat родителя и brief==description);
   они не «угадывают» атомарность текста, а реализуют правило protocol §3.7.
8. **Логируем честно** — think и final пишутся всегда, даже если downstream
   использует только final (§2.4).
9. **Каждый плагин = unit-тесты с fake LLM/fake memory**, coverage >80%.
   Fake-память — обычные `Memory`-экземпляры (все четыре dict'ом, §6.1);
   fake LLM — callable/строка на seam'е `Agent.llm` (§6.4).

### 12.3 Матрица обратной совместимости API

| API | Гарантируется стабильным? | Примечание |
|-----|---------------------------|------------|
| Memory get/set/clear + subscribe/unsubscribe (glob) | да (semver minor) | ключи — domain плагинов; уровни задаются экземпляром, суффиксов не вводим; скоуп подписки строго per-level (вариант А, §5.4) — без обёрток и propagation |
| Иерархия Workspace/Project/Agent (add_*, *_memory свойства) | да (semver minor) | project/workspace могут быть None у невстроенного агента |
| Сигнатуры on_input/on_upload/on_output | да (semver major на ломку) | новые optional-параметры — minor |
| bind(workspace/project/global/local_memory) | да (semver major на ломку) | порядок/наборы keyword-аргументов фиксированы |
| Результат runtime `{"op": ...}` | да | новые op добавляются расширением enum |
| Результат run() (dict: response/context/segments/parsed/metadata/action) | нет до 1.0.0 | поля добавляются; удаление — major |
| PersistentStore API (set/get/all/clear) | да (semver minor) — ПЛАН | схема таблицы `memory(namespace,key,value,updated_at)` |
| BreakpointManager JSONL-формат | нет до 1.0.0 — ПЛАН | при смене формата — версионирование записи `v` |
| PLUGIN_REGISTRY | да — ПЛАН | имена встроенных плагинов не переиспользуются |

### 12.4 Roadmap плагинного слоя

| Версия | Что добавляется |
|--------|-----------------|
| 0.1.x (сейчас) | done: base-классы (Pre/Runtime/Post) + 4-уровневая память + StreamParser + Agent.run + TaskParserPlugin |
| 0.2.x | retry/reject-цикл в Agent (max_retries, AgentRejectedError); изоляция падений плагинов (§9.3); snapshot()/breakpoints (#3) |
| 0.3.x | Персистентность: sqlite-бэкенд за тем же API `Memory` + namespace'ы (§6.5–6.6); SessionCleanup |
| 0.4.x | Каталог §7 по одному (HistoryLogger, StatisticsCollector, EarlyStopper…); PLUGIN_REGISTRY + TOML-конфигурации (§8, §12.1); entry points |
| 1.0.0 | Заморозка API секций; плагины — единственный способ расширения агентов |

---

*Документ описывает архитектуру v3 (плагинный слой). Фактическое поведение
ядра v2 задают: docs/protocol.md (протокол), docs/process.md (конвейер),
docs/data.md (данные), docs/prompts.md (промпты).*

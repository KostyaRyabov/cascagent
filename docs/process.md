# cascagent — Процесс выполнения: ленивая декомпозиция, обогащение, агенты обратной связи

> Статус: **проект v2**. Документ детализирует Главу IX полной документации
> (процесс работы системы) применительно к протоколу v2. Формат общения с
> LLM — `docs/protocol.md`; реестр CPU-шагов — `docs/cpu-offload.md`;
> модель данных и хранилища — `docs/data.md`; промпты агентов —
> `docs/prompts.md`; системные агенты и state machine — `docs/product.md` §4–5.
> Расхождения оригинала Главы IX с v2 помечены «⚠️ v1-наследие» и разобраны
> в §8.

## 1. Ленивая декомпозиция (lazy decomposition)

**Принцип:** дерево задач строится **не заранее**, а по мере выполнения.
Узел декомпозируется ровно один раз — непосредственно перед исполнением его
детей, на актуальном контексте.

Почему не строить полное дерево сразу (аргументы Главы IX, приняты):
1. **Контекст растёт по мере выполнения** — результаты детей обогащают
   контекст следующих узлов (§2); ранняя декомпозиция работает на догадках.
2. **Ранние декомпозиции могут стать неверными** — если 0.1 провалилась или
   изменила подход, ветки 0.2–0.5 могут быть бессмысленны; при ленивой
   схеме они просто никогда не будут разложены (экономия токенов).
3. **Экономия токенов** — не тратим вызовы на отменённые/несостоявшиеся ветки.
4. **Качество** — каждый уровень видит реальный мир (файлы, версии, ошибки),
   а не предположение о нём.

Обоснование и недостатки — ADR-004 (`docs/decisions/004-lazy-decomposition.md`):
нет общего обзора плана заранее, сложнее планировать параллелизм (сейчас
single-flight, поэтому несуществующая проблема).

### 1.1. Эталонный алгоритм (v2, соответствует protocol §7)

```python
def execute_task(task: Task, context: EnrichedContext | None = None) -> str:
    """Рекурсивное выполнение задачи с ленивой декомпозицией. Чистый CPU-код."""
    if task.status == TaskStatus.DONE:
        return task.result                      # идемпотентность / resume
    # PENDING → ENRICHMENT: именно здесь задача начинает работу и обогащается
    # контекстом и ресурсами; в PENDING обогащения НЕ происходит (data.md §1,
    # TaskStatus; product §5)
    kanban.update_status(task.id, TaskStatus.ENRICHMENT, started_at=datetime.now(timezone.utc))

    # 1. Обогащение контекста (CPU, §2) — этап ENRICHMENT: выбор агента-исполнителя,
    #    MCP-инструменты, базы знаний/recall, формирование контекста
    enriched = enricher.enrich(
        brief=task.brief,
        parent_context=context,
        completed_siblings=kanban.done_siblings(task.id),
    )
    # ENRICHMENT → RUNNING: контекст собран, начинаем LLM-вызовы
    kanban.update_status(task.id, TaskStatus.RUNNING)

    # 2. Декомпозиция — только если детей ещё нет (ленивость)
    if not task.subtasks:
        call = decomposer.decompose(task, enriched)   # isolated HTTP call
        history.save(call)                             # THINK+FINAL, честный лог
        if call.is_atomic or detector.is_parent_repeat(task.brief, only_child):
            result = executor.execute(task, enriched)  # атом → Executor (§5)
            kanban.update_status(task.id, TaskStatus.DONE, result=result)
            semantic.remember(task.brief, result, task.id)
            return result
        children = dedup(filter_dupes(parse(call.final_response), task))
        for i, sub in enumerate(children):             # Task(...) уже содержит
            sub.order = i                              # snowflake id и created_at (data.md §2)
            sub.parent, sub.depth = task, task.depth + 1  # прямые ссылки (data.md §1)
            kanban.add_task(sub)                       # в SQL — parent_id (wire-формат)
        task.subtasks = children                       # List[Task], порядок = исполнение

    # 3. Последовательное выполнение детей (порядок = зависимости, A9/A10)
    results = []
    for i, child in enumerate(task.subtasks):          # живые объекты, kanban.get не нужен
        if child.status == TaskStatus.DONE:
            results.append(child.result)               # resume без инференса
            continue
        try:
            results.append(execute_task(child, enriched))
        except TaskFailedError as e:
            reflector.handle_failure(child, e,
                                     remaining_siblings=task.subtasks[i + 1:])
            raise                                      # решение Reflect — ниже (§6)

    # 4. Сборка результата вверх по дереву (A12)
    final_result = summarize_results(results, task)
    kanban.update_status(task.id, TaskStatus.DONE, result=final_result)
    semantic.remember(task.brief, final_result, task.id)
    return final_result
```

Отличия от листинга Главы IX (осознанные, v2):
- статусы `TaskStatus.{PENDING,ENRICHMENT,RUNNING,DONE,FAILED}` вместо
  `CANCELLED` (отмена ветки = FAILED с `partial=true`, product §5);
  обогащение контекста/ресурсов — отдельный этап ENRICHMENT между стартом
  и первым LLM-вызовом (data.md §1);
- degenerate-repeat проверка идёт **после** парсинга (правило CPU,
  protocol §3.7), а не как отдельный ответ модели;
- `RESEARCH_NEEDED` удалён из протокола модели — триггер исследования
  решает enricher по `gaps/confidence` (ADR-003), см. §5;
- история пишется на каждом вызове (`history.save(call)`), включая провальные.

### 1.2. Временная линия (из Главы IX, без изменений по смыслу)

```
T0: запрос "Создать REST API"          → задача id=1 (pending)
T1: декомпозиция L0                     → 1 → [2 … 6]   (дети, order 0..4)
T2: задача 3 декомпозируется на актуальном контексте → [7, 8] → атоны ✓
    3 DONE, result="FastAPI + PostgreSQL настроены"
T3: задача 4 декомпозируется с результатом 3 в контексте → [9..11] ...
T4: задача 5 видит результаты 3+4 — и т.д.
```

Инвариант: в момент T3 узел 4 ещё **не имеет** детей в Kanban — дети
появляются только когда 4 начинает исполняться. Это и есть ленивость.
(Идентификаторы — snowflake, растущие со временем; родство видно только
через `parent_id`/`order`, не через сам id — data.md §2.)

### 1.3. Конвейер пост-обработки ответа декомпозиции (плагин TaskParser)

Ответ Decomposer-агента превращается в узлы дерева задач **одним**
output-плагином `TaskParser` (v3, plugins §7.4; вся логика — чистый CPU,
LLM не участвует). Цель — снять с LLM всю механику: модель только
генерирует текст по формату protocol §2–3, остальное детерминировано.

Плагин исполняется как **три строго упорядоченные стадии**; порядок важен:
сохранение в `tasks` происходит только ПОСЛЕ фильтрации, чтобы в дереве
не появлялись промежуточные мусорные узлы (ранее планировавшийся
отдельный `TaskFilter` объединён с парсером — стадии идут в одном
проходе, эмбеддинги не перекодировались бы между плагинами, а разделение
на два плагина требовало бы писать в `tasks` дважды).

**Стадия 1 — парсинг.** Разбирает final_response по грамматике протокола
(эталон — protocol §3) и строит кандидатов `Task`; при инициализации
каждой задачи считается `embedding = embed(brief)` (brief — первая
строка блока = название задачи; ровно одна эмбеддинг-прогонка на задачу,
дальше вектор только переиспользуется). У атома (`<atom>` второй строкой
или глобальный `<atom>`) `description = None`, `is_atom = True`.

**Стадия 2 — фильтрация/проверка** (правила F1–F3, порядок фиксирован,
все проверки — CPU, без обращения к модели; работают по эмбеддингам из
стадии 1):

| Правило | Проверка | Действие |
|---|---|---|
| **F1. Самодостаточный атом** | `normalize(brief) == normalize(description)` (или высокосходны: `cos(embed(brief), embed(description)) ≥ 0.90` — сравнение считается **на лету внутри F1**, отдельный `embedding(description)` в модели не хранится, см. data.md §1 п.2) | задача уже «название = что делать» → `is_atom = True`, `description = None` |
| **F2. Повтор родителя** | `similarity(embed(child.brief), embed(parent.brief)) ≥ 0.75` (threshold — config `[dedup]`, algorithms §2) | дочерняя задача **удаляется** из списка детей |
| **F3. Схлопывание родителя** | после F2 у родителя не осталось ни одной дочерней задачи | родитель помечается `is_atom = True` (декомпозиция вырождена → отдаём его Executor'у без нового LLM-вызова) |

**Стадия 3 — сохранение в переменную `tasks`.** В процессе декомпозиции
project-ключ **`tasks`** (SessionMemory, plugins §5.7; персистентное
зеркало — Kanban, data.md §4.2) **уже заполнен** — дерево существует к
моменту вызова. Поэтому плагин НЕ перезаписывает `tasks`, а **подставляет
полученное поддерево отфильтрованных детей нужному родителю**: присваивает
`parent.subtasks = [объекты детей]` и добавляет новые узлы в словарь `{id:
Task}` одним атомарным `update_project`.

```python
def on_output(self, response, context, session, memory):
    # стадия 1: парсинг + Task(...) — конструктор сам фиксирует created_at и
    # генерирует snowflake id (data.md §2); embedding(brief) — при инициализации
    parsed = parse_decomposition(response)          # protocol §3: блоки через пустую строку
    children = []
    for p in parsed:                                # p: {brief, description|None, is_atom}
        t = Task(
            brief=p["brief"],
            description=None if p["is_atom"] else p["description"],
            is_atom=p["is_atom"],                   # <atom> второй строкой / глобальный <atom>
            order=len(children),                    # позиция = порядок в ответе LLM
        )                                           # __post_init__: created_at=datetime.now(utc),
                                                    # id=snowflake_ids.next_id(created_at)
        t.embedding = self.embedder.encode(t.brief) # одна прогонка на задачу, дальше переиспользуется
        children.append(t)

    # стадия 2: фильтрация/проверка (F1–F3) — до сохранения
    parent = memory.get_project("tasks")[context["parent_id"]]
    kept = []
    for ch in children:
        if ch.is_atom:                                      # <atom> уже расставлен парсером
            kept.append(ch); continue
        if same_or_redundant(ch.brief, ch.description):     # F1: сравнение на лету,
            ch.is_atom, ch.description = True, None         # embedding(description) НЕ хранится
        if cosine(ch.embedding, parent.embedding) >= THRESHOLD:  # F2 (эмбеддинги стадии 1)
            continue                                        # дубль родителя — удаляем
        kept.append(ch)
    if not kept and not parent.is_atom:                     # F3
        parent.is_atom, parent.description = True, None

    # стадия 3: сохранение — подставить поддерево к родителю в уже живое дерево
    def attach(d):                                          # d: {id: Task}, дерево уже заполнено
        for i, ch in enumerate(kept):                       # id уже snowflake (стадия 1)
            ch.order = i                                    # переиндексация после фильтрации F2
            ch.parent = parent                              # прямые ссылки на объекты (v2+)
            ch.depth = parent.depth + 1                     # инкремент от родителя
        parent.subtasks = kept                              # List[Task], порядок = исполнение
        return d | {ch.id: ch for ch in kept} | {parent.id: parent}
    memory.update_project("tasks", attach)
    return {"parsed": kept, "action": "accept"}
```

Связь с ядром v2: F1/F2 реализуют правило degenerate-repeat (protocol §3.7,
cpu-offload A5) и частично дедупликацию A6 — в v3 они вынесены из
оркестратора в плагин и работают **по эмбеддингам, посчитанным на стадии
1**, поэтому повторного кодирования нет (экономия CPU и детерминизм).
F3 закрывает цикл «родитель → один такой же ребёнок → снова декомпозиция»:
вместо второго LLM-вызова родитель становится атомом. Порядок секций
output для Decomposer: `TaskParser → DuplicateFilter → HistoryLogger`
(plugins §8.1).

## 2. Context Enrichment Pipeline

**Когда:** перед каждым вызовом LLM (декомпозиция, executor, reflect).
Модель не знает источников — она видит блок `Контекст:` (prompts §3).

**Ключевой принцип (зафиксирован 2026-10-10):** LLM сама НЕ решает что
искать. Обогащение — чистая CPU-предобработка: у задачи есть `brief`,
по его эмбеддингу идёт семантический поиск по всем индексам проекта
(документация, файлы, база знаний, findings исследований), top-K
фрагментов подмешивается в контекст **до** передачи в LLM. Дефолтный
путь поиска — векторный (embedding brief'а → cosine similarity); BM25
в RAG остаётся дешёвой предфильтрацией кандидатов (algorithms §4), а не
способом «сформулировать запрос». Детерминированность: одинаковый
brief → одинаковое обогащение. Нехватку данных определяет не модель, а
Validator **после** попытки выполнения (§5, roadmap §4.6.2).

```python
class ContextEnricher:
    def enrich(self, brief: str, parent_context=None,
               completed_siblings=None, max_tokens: int = 500) -> EnrichedContext:
        ctx = EnrichedContext()
        qv = self.embedder.encode(brief)   # embedding(brief) — ЕДИНЫЙ query для всех индексов
        ctx.rag_documents      = [d["content"] for d in rag.search(qv, top_k=3)]     # P3, vector-first
        ctx.semantic_memories  = format_memories(semantic.recall(qv, top_k=3))       # P2, cosine по эмбеддингам
        ctx.similar_tasks      = kanban.find_similar(qv, top_k=3)                            # P1/P2
        ctx.completed_siblings = completed_siblings or []
        ctx.parent_context     = parent_context or {}
        full = ctx.to_prompt_block(max_tokens)                                           # data.md §6
        if approx_tokens(full) > max_tokens:                                             # P5
            full = selective.compress(full, max_tokens)
            ctx.compressed = True
        ctx.total_tokens = approx_tokens(full)
        ctx.gaps = detect_gaps(ctx)   # пусто везде → сигнал ResearchAgent (§5)
        return ctx
```

Схема потока (зафиксирована): `brief → embed(brief) → semantic_search по
индексам [docs, files, KB, research findings] → format(top-K) → LLM
работает только с готовым контекстом → Validator проверяет результат;
при нехватке данных инициирует Researcher, тот обогащает индексы, и
Executor повторяет попытку`. Разделение ответственности: CPU решает
**что релевантно** (similarity), LLM — **как использовать**, Researcher —
**где искать новое**, Validator — **хватает ли**. Никаких «запросов к
LLM о том, что поискать» — это исключает зацикливание на выборе поиска.

Приоритет при обрезке после сжатия (data.md §6): siblings > research >
semantic > RAG. Порядок фиксирован — deterministic, тестируется без сети.

**Selective Context** — сторонняя библиотека сжатия промптов (extras
`enrich`, config.md §2): удаляет low-infofuzz предложения, сохраняет
сущности/термины; типичная компрессия 50–70%. Fallback без extras —
собственный gzip+частотный фильтр (algorithms.md §0.2).

Стоимость pipeline'а: десятки миллисекунд CPU против 10–20 с GPU-вызова —
всегда оправдан (cpu-offload §4).

## 3. Протокол передачи контекста между уровнями

`execute_task` спускает вниз только `EnrichedContext` родителя плюс
чеклист DONE-siblings. Никакой полной истории чата (изоляция агентов,
product §2.1). Структура USER-блока — protocol §5 / prompts §3.

## 4. Суммаризация результатов (A12)

После завершения всех детей родитель собирает результат:

| Ситуация | Механизм | LLM? |
|---|---|---|
| Σ len(results) < budget (~2000 симв.) | конкатенация `'\n\n---\n\n'` | нет |
| превышен budget | Selective Context сжатие | нет |
| сжатие не помогло (нужен структурно связный отчёт) | SummarizerAgent (prompts §5) | да, редкий fallback |

Результат пишется в `task.result` (Kanban) и `semantic.remember(brief,
result)` — будущие recall. Правило: суммаризация **не вызывает модель**
по умолчанию; LLM-summarize — исключение с бюджетом 1 вызов на узел.

## 5. Исследовательские задачи (ResearchAgent)

⚠️ v1-наследие: в Главе IX декомпозер возвращал маркер `RESEARCH_NEEDED`.
В v2 модель **не умеет** просить исследование (ADR-003): триггер чисто CPU —
`enriched.gaps` непустой (семантический поиск по эмбеддингу brief'а ничего
не нашёл во всех индексах; см. §2) либо Executor вернул ошибку «нет входных
данных», либо Validator после проверки результата вынес вердикт
«нехватка данных» → инициирует Researcher (roadmap §4.6.2). Роли разведены
жёстко: **Executor** выполняет задачу только с теми ресурсами что дал
enrichment; **Researcher** ищет НОВЫЕ ресурсы и обогащает индексы для
будущих задач; **Validator** решает хватает ли ресурсов.

```python
def handle_research_needed(brief: str) -> ResearchFinding:
    query = normalize(formulate_query(brief))          # ключ кэша P1-style
    if cached := research_cache.lookup(query):         # sha256(normalize) — data.md §5
        return cached
    agent = ResearchAgent(tools=[WebSearchTool(), DocsSearchTool(),
                                 DBQueryTool(), FileReadTool(), ASTSearchTool()],
                          max_iterations=5)            # защита от зацикливания
    finding = agent.run(query)                         # isolated slot, свой промпт
    research_cache.save(query, finding)                # повтор = 0 LLM-вызовов
    return finding
```

Изоляция: собственный короткий system prompt (prompts §5), только тема
исследования, вывод ≤ K токенов, confidence 0–1. findings попадают в
следующий `EnrichedContext.research_findings` — декомposer не знает, что
это был research (блок «Контекст» однороден).

## 6. Обратная связь от выполнения (ReflectAgent)

Триггеры (product §4): FAILED атома после 1 retry; degenerate-цикл
декомпозиции; enrichment пуст при явной нехватке знаний.

Выход v2 — однострочный контракт (prompts §5): `retry` / `reframe: …` /
`give_up: …`. Оркестратор применяет:

```python
def apply_reflect(decision: ReflectDecision):
    if decision.action == "retry":
        kanban.update_status(task.id, TaskStatus.PENDING)          # 2-я попытка
    elif decision.action == "reframe":
        task.brief, task.description = decision.new_brief, decision.new_desc
        kanban.update_task(task)                                   # depth+1, max 2 цикла
        kanban.update_status(task.id, TaskStatus.PENDING)
    else:  # give_up
        kanban.update_status(task.id, TaskStatus.FAILED, result=decision.diagnosis)
        cancel_subtree(task)      # дети → CANCELLED-пометка в metadata, не в статусе
```

⚠️ v1-наследие: `action=replan` с полем `tasks_to_cancel/new_tasks` из
Главы IX. В v2 REPLAN декомпозирован: reframe переписывает **текущую**
задачу (новая версия, не новый список), отмена будущих siblings —
побочный эффект сворачивания ветки (give_up). Возвращать «список новых
задач» модели запрещено — это планирование, а планирование на CPU
(cpu-offload §3 «запрещено просить модель»). Бюджет: максимум 2 вызова
Reflect на задачу (stack §3).

## 7. Обработка отказов (сводка правил cpu-offload §2.3)

```
атом failed → retry×1 (тот же промпт) → ReflectAgent → {retry|reframe|give_up}
root с FAILED-веткой → завершается с partial=true в результате
краш процесса → resume из Kanban: RUNNING и ENRICHMENT→PENDING при старте, DONE не перевыполняется
```

## 8. Расхождения Главы IX с v2 (реестр)

1. `RESEARCH_NEEDED` от модели → CPU-триггер по gaps (ADR-003).
2. Категории `> ! ?` в примерах → удалены.
3. `TaskStatus.CANCELLED` → нет такого статуса; FAILED + metadata-пометка.
4. Reflect `replan` со списком новых задач → reframe одной задачи; планирование списка — оркестратор.
5. `semantic_memory.remember` эмбеддит `brief`, не description (algorithms §3).
6. Контекст max_tokens=500 — совпадает с `[enrichment].max_context_tokens` (config §4).
7. Примеры вывода CLI (Глава XII) используют v1-лейблы категорий — при
   написании `cli.py` рендер дерева: `✓/✗/…` по статусу, без иконок категорий.

## 9. Открытые вопросы

- Формулировка `research query` из brief: шаблон CPU или 1 LLM-вызов?
  (таргет — шаблон, без вызова).
- Каскадный reframe: допускается ли reframe родителя, если все дети DONE
  (частичный успех)? Пока — нет, только по FAILED.
- Порог `detect_gaps`: считать ли пустым recall при top-1 similarity ≥ 0.6?

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
    kanban.update_status(task.id, TaskStatus.RUNNING, started_at=now())

    # 1. Обогащение контекста (CPU, §2) — перед КАЖДЫМ LLM-вызовом
    enriched = enricher.enrich(
        brief=task.brief,
        parent_context=context,
        completed_siblings=kanban.done_siblings(task.id),
    )

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
        for i, sub in enumerate(children):
            sub.id = f"{task.id}.{i + 1}"              # dot-path, data.md §2
            sub.parent_id, sub.depth = task.id, task.depth + 1
            kanban.add_task(sub)
        task.subtasks = [c.id for c in children]

    # 3. Последовательное выполнение детей (порядок = зависимости, A9/A10)
    results = []
    for cid in task.subtasks:
        child = kanban.get(cid)
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
- статусы `TaskStatus.{PENDING,RUNNING,DONE,FAILED}` вместо `CANCELLED`
  (отмена ветки = FAILED с `partial=true`, product §5);
- degenerate-repeat проверка идёт **после** парсинга (правило CPU,
  protocol §3.6), а не как отдельный ответ модели;
- `RESEARCH_NEEDED` удалён из протокола модели — триггер исследования
  решает enricher по `gaps/confidence` (ADR-003), см. §5;
- история пишется на каждом вызове (`history.save(call)`), включая провальные.

### 1.2. Временная линия (из Главы IX, без изменений по смыслу)

```
T0: запрос "Создать REST API"          → задача 0 (pending)
T1: декомпозиция L0                     → 0 → [0.1 … 0.5]
T2: 0.1 декомпозируется на актуальном контексте → [0.1.1, 0.1.2] → атоны ✓
    0.1 DONE, result="FastAPI + PostgreSQL настроены"
T3: 0.2 декомпозируется с результатом 0.1 в контексте → [0.2.1..0.2.3] ...
T4: 0.3 видит результаты 0.1+0.2 — и т.д.
```

Инвариант: в момент T3 узел 0.2 ещё **не имеет** детей в Kanban — дети
появляются только когда 0.2 начинает исполняться. Это и есть ленивость.

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
краш процесса → resume из Kanban: RUNNING→PENDING при старте, DONE не перевыполняется
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

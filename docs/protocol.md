# cascagent — Протокол декомпозиции (v2, ультра-простая архитектура)

> Статус: **проект v2** (spec принят, код мигрируется).
> Этот документ — источник истины по формату общения с LLM.
> Системный промпт модели зафиксирован в `parser.SYSTEM_PROMPT` —
> менять текст можно только вместе с тестами.

## 1. Философия

Модель **не знает** про категории, RAG, semantic memory, enrichment,
валидаторы и Kanban. Она видит только короткий запрос «разбей задачу»
и отвечает простым текстом. Вся остальная логика — детерминированный
CPU-слой вокруг неё (см. §5–7 и docs/architecture.md).

Размер system prompt: ~480 символов (было ~2000 в v1).

## 2. System prompt (фиксируем дословно)

```
Разбей задачу на подзадачи следующего уровня детализации.

ФОРМАТ ОТВЕТА:
1. Название задачи одной строкой
2. Описание что нужно сделать

Между подзадачами — ПУСТАЯ СТРОКА (разделитель).

ПРАВИЛА:
- Подзадачи должны полностью покрывать исходную задачу
- Каждая подзадача — конкретное действие
- Не повторяй исходную задачу
- Если подзадача уже атомарна (одно простое действие) — в описании напиши только: <atom>
- Если ВСЯ исходная задача не делится — верни только одну строку: <atom>
```

Хранится как константа `SYSTEM_PROMPT` в `cascagent/plugins/task_parser.py`.
Стабильный префикс → llama.cpp prefix caching / KV save-restore работает
на нём без модификаций.

Обоснование минимализма, эволюция версий промпта и экономия токенов —
**`docs/prompts.md §2/§7` и ADR-002** (здесь не дублируются).

## 3. Формат вывода модели

Каждая подзадача — две строки, подзадачи разделяются пустой строкой
(согласно блоку «ФОРМАТ ОТВЕТА» системного промпта, §2):

```
Название задачи            <- строка без отступа, первая в блоке подзадачи
Описание что нужно сделать <- следующая строка того же блока

                           <- ПУСТАЯ СТРОКА = разделитель подзадач
```

Пример ответа:

```
Создать модель User в базе данных
Определить поля: id (UUID, primary key), email (string, unique),
password_hash (string), created_at (timestamp). Создать SQLAlchemy модель.

Настроить миграции Alembic
Инициализировать Alembic, создать первую миграцию с таблицами users и posts.

Подготовить сиды
<atom>
```

Атомарность — на двух уровнях (оба детектируются точным `<atom>`,
без эвристик):
- **построчно**: описание атомарной подзадачи = ровно `<atom>` →
  подзадача с `description = None` и флагом `is_atom = True`;
- **глобально**: весь ответ = ровно одна строка `<atom>` → исходная задача
  не делится (дети не создаются).

### Правила парсинга (инварианты)

1. **Атомарность — только точное совпадение `<atom>`** (case-insensitive,
   после удаления markdown-шума) или пустой final_response. Никаких
   эвристик вида «короткая строка = атом». Два уровня: весь ответ =
   `<atom>` → глобальный атом; вторая строка блока = `<atom>` →
   атомарная подзадача (`description = None`, `is_atom = True`).
2. **Разделитель подзадач — пустая строка**: первый блок ответа = задача
   целиком (глобальный `<atom>` либо preamble); далее каждый непустой
   блок (группа строк между пустыми строками) = одна подзадача.
3. **Структура блока**: первая строка = `brief`, остальные строки =
   `description`; склеиваются через одиночный пробел (переносы внутри
   описания — артефакт переноса, а не структура). У атома `description`
   нормализуется в `None` (пустое описание), `is_atom = True` — модель
   при этом обязана вернуть `<atom>`; парсер никогда не заменяет `<atom>`
   эвристиками и наоборот.
4. **Толерантность к отступам (обратная совместимость)**: если модель
   вернула старый отступной формат («Название» + строки с отступом без
   пустых строк), парсер принимает его: leading-whitespace строки =
   продолжение description. Пустая строка всегда закрывает текущую
   подзадачу в обоих форматах.
5. **Никогда не обрезаем список** подзадач: возвращаем всё, что сгенерировано
   (фильтрация дублей — ответственность DuplicateDetector выше по стеку).
6. Парсер не изобретает задач: preamble до первой задачи игнорируется; но
   ни одна валидная пара brief+description не теряется.
7. **Защита от вырожденного ответа:** если после разбора осталась ровно
   одна подзадача, а её `brief` высокосходен с задачей родителя
   (DuplicateDetector.is_parent_repeat) — считаем, что модель повторила
   задачу вместо декомпозиции: это атом. Правило детерминировано и живёт
   на CPU, модель про него не знает.
8. Markdown-шум на границах строк sanitize'ится детерминированно
   (буллеты, нумерация, `**`/backtick обёртки, zero-width символы) —
   `sanitize_line()` переиспользуется из v1 без изменений.

Эталонная реализация (~25 строк):

```python
def parse_decomposition(response: str) -> List[Task]:
    if response.strip().lower() == "<atom>":       # глобальный атом
        return []
    tasks = []
    for block in re.split(r"\n\s*\n", response.strip()):  # пустая строка — разделитель
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        brief, rest = lines[0], lines[1:]
        if len(rest) == 1 and rest[0].lower() == "<atom>":  # построчный атом
            tasks.append(Task(brief=brief, description=None, is_atom=True))
        else:
            tasks.append(Task(brief=brief, description=" ".join(rest) or None))
    return tasks
```

Конструктор `Task` сам формирует остальные поля за один шаг (`__init__`,
data.md §1): фиксирует `created_at = datetime.now(timezone.utc)`, кодирует из
него snowflake `id`, считает `embedding(brief)` и вычисляет `depth` из
переданного `parent` — парсер их не заполняет.

Парсер — только первый шаг конвейера; инициализация эмбеддингов задач
(`embedding = embed(brief)` при создании каждого `Task`) и CPU-фильтрация
результатов (правила F1–F3: brief==description ⇒ атом, дубль родителя ⇒
удаление ребёнка, нет детей ⇒ родитель становится атомом) — в **process §1.3**.

## 4. Модель данных (упрощённая)

**Единственный источник истины по модели данных — `docs/data.md` §1–2**
(полное определение `Task`/`TaskStatus` с полями времени, обоснование,
эволюция v1→v2, dot-path схема ID). Здесь — только контракт протокола:

- модель оперирует парой `brief` (название, для RAG/дерева) +
  `description` (подробное описание, для LLM); статусы
  `pending | enrichment | running | done | failed` (pending = работа ещё не
  начата; enrichment = старт: сбор контекста/ресурсов до LLM-вызова,
  semantics — data.md §1, product §5);
  дети — список id в SQL-контракте, в памяти — объекты `Task`
  (`subtasks: List[Task]`, порядок = исполнение, position == index; поля
  `order` в модели нет — data.md §1);
- **УДАЛЕНО из v1** (определяется системой, а не моделью):
  `TaskCategory` (`>` / `!` / `?`), acceptance criteria, `blocked_by`
  (обоснование отказа — data.md §1, ADR-003);
- `DecompositionCall` остаётся (task_id, task_brief, depth, think_enabled,
  prompt, think, final_response, timings, error, atomic) — формат JSONL
  истории совместим между v1/v2; спецификация записи — data.md §3.

## 5. Что видит модель (полный prompt одного вызова)

```
SYSTEM:
§2 (системный промпт, ~480 символов)

USER:
Задача: {task.brief}

Контекст:
{enriched_context}                 # собран enricher'ом, модель не знает источник

Выполненные ранее:
- {completed_siblings} ✓

РАЗБЕЙ НА ПОДЗАДАЧИ:
```

Референсная сборка — `parser.build_user_prompt(brief, context_lines,
completed_siblings)` (pure function, тестируется без сети).

- Название родителя входит в `Контекст:` только когда enricher его добавил.
- think/no_think выбирается системой по глубине (политика и механика —
  **`docs/stack.md §5`**, cpu-offload P7) — модель не управляет этим сама.

## 6. Роль CPU-слоя (модель этого не знает)

Общая схема: перед вызовом контекст собирает ContextEnricher, после вызова
вывод разбирает и валидирует оркестратор, после выполнения результаты
суммируются и запоминаются. **Детализация каждого шага в этом документе не
дублируется** — исчерпывающие источники:

| Аспект | Источник истины |
|---|---|
| Полный реестр CPU-шагов (pre-call P1–P9, post-call A1–A13, ошибки/ретраи) с модулями и стоимостью | `docs/cpu-offload.md` |
| Конвейер обогащения (RAG + recall + Selective Context + siblings), эталонный код | `docs/process.md` §2 |
| Системные агенты (триггеры, входы/выходы) | `docs/product.md` §4 |
| Модель данных и хранилища (Task v1→v2, dot-path ID, JSONL, схема SQLite, EnrichedContext) | `docs/data.md` |
| Промпты всех агентов и разбор вывода | `docs/prompts.md` |
| Обоснования решений | `docs/decisions/` |
| Процесс выполнения целиком (ленивая декомпозиция, Research/Reflect, суммаризация) | `docs/process.md` |
| Сквозные примеры логов | `docs/examples.md` |
| Эксплуатация | `docs/operations.md` |

## 7. Псевдокод ядра

Сокращённая схема (только протокольные развилки: атом/degenerate-repeat →
Executor, иначе parse+dedup → дети; суммаризация вверх). **Эталонный
полный алгоритм ленивого выполнения (`execute_task` с enrich/kanban/
history/reflect-ветками) — `docs/process.md §1.1`, там же отличия от
Главы IX:**

```python
def execute_with_decomposition(task: Task) -> str:
    if task.status == DONE:
        return task.result
    if not task.subtasks:
        enriched = enricher.enrich(task.brief)
        call = decomposer.decompose(task, enriched)      # isolated HTTP call
        if call.is_atomic or degenerate_repeat(call, task):   # §3.7
            task.result = executor.execute(task)
            task.status = DONE
            return task.result
        task.subtasks = filter_dupes(parse(call.final_response), task)  # List[Task]
        kanban.save_all(task.subtasks)
    results = [execute_with_decomposition(c) for c in task.subtasks]
    task.result = summarize_results(results)
    task.status = DONE
    kanban.update(task)
    return task.result
```

## 8. Отличия v1 → v2

**Единственный источник истины по миграции и эволюции — `docs/data.md` §1
(эволюция модели Task), `docs/prompts.md §2/§7` (эволюция промпта) и ADR-003.**
Там же причины отказа (модель путалась в символах категорий, лишние токены,
недетерминированные acceptance criteria). Здесь — только перечень того, что
изменилось на уровне протокола: удалены категории `> ! ?` и маркер
`RESEARCH_NEEDED`, вывод — блоки «brief + описание», разделённые пустой строкой, + двухуровневый `<atom>`, system
prompt минимален (~110 токенов), поля `title/acceptance/blocked_by` заменены на
`brief/description/порядок siblings (позиция в subtasks)`.

## 9. Миграция кодовой базы (чеклист)

- [x] Документация v2 принята (этот файл, AGENTS.md §2.3–7/§10, architecture.md)
- [ ] `models.py`: `TaskStatus`, новый `Task(brief, description, ...)`,
      удалить `TaskCategory`/`SYMBOL_TO_CATEGORY`; `DecompositionCall` под v2
- [ ] `parser.py`: `SYSTEM_PROMPT`, `ATOM_MARKER`, `is_atomic()`,
      `parse_decomposition()`, `build_user_prompt()`;
      удалить `detect_category`, `parse_response`, старую сборку prompt;
      `split_think_and_response`, `sanitize_line` — без изменений
- [ ] `detector.py`: API на `task.brief` вместо `task.title` (логика та же)
- [ ] Тесты parser/detector переписать под v2-формат (+тест на §3.7)
- [ ] history.py — по спецификации v2 (JSONL + debug.log, THINK/FINAL RESPONSE)
- [ ] Этапы 2–6 (client, cache_manager, decomposer/kanban/cli, bk_tree,
      semantic, rag, enricher/executor/reflector/researcher)
      — по AGENTS.md §6–7

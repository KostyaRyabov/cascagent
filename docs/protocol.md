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

Размер system prompt: ~200 символов (было ~2000 в v1).

## 2. System prompt (фиксируем дословно)

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

Хранится как константа `SYSTEM_PROMPT` в `cascagent/parser.py`.
Стабильный префикс → llama.cpp prefix caching / KV save-restore работает
на нём без модификаций.

## 3. Формат вывода модели

Каждая подзадача — два блока текста:

```
Название задачи            <- строка БЕЗ отступа (level-0 line)
    Подробное описание     <- одна или несколько строк С отступом
```

Пример ответа:

```
Создать модель User в базе данных
    Определить поля: id (UUID, primary key), email (string, unique),
    password_hash (string), created_at (timestamp). Создать SQLAlchemy
    модель с async поддержкой.

Настроить миграции Alembic
    Инициализировать Alembic, создать первую миграцию с таблицами
    users и posts.
```

Атомарная задача — ровно один токен в ответе: `<atom>` (в нижнем регистре
после sanitize; допускаются обрамляющие пробелы/пустые строки).

### Правила парсинга (инварианты)

1. **Атомарность**: только точное совпадение `<atom>` (case-insensitive,
   после удаления markdown-шума) или пустой final_response. Никаких
   эвристик вида «короткая строка = атом».
2. **Строка без отступа** = начало новой подзадачи (`brief`).
3. **Строки с отступом** = описание текущей подзадачи; склеиваются через
   одиночный пробел в один `description` (переносы строк внутри описания
   — артефакт переноса, а не структура).
4. **Никогда не обрезаем список** подзадач: возвращаем всё, что сгенерировано
   (фильтрация дублей — ответственность DuplicateDetector выше по стеку).
5. Парсер не изобретает задач: preamble до первой задачи игнорируется; но
   ни одна валидная пара brief+description не теряется.
6. **Вырожденный ответ = атом.** Если единственная подзадача декомпозиции
   повторяет название задачи-родителя (`brief` ≈ родитель, см. §3.8 правило R1)
   — это не декомпозиция, а признание нерасчленённости: задача атомарна.
   Правило детерминировано и живёт на CPU, модель про него не знает.
7. Markdown-шум на границах строк sanitize'ится детерминированно
   (буллеты, нумерация, `**`/backtick обёртки, zero-width символы) —
   `sanitize_line()` переиспользуется из v1 без изменений.

### 3.8 Дедупликация: два разных случая (правила R1–R2)

Совпадение названий бывает двух принципиально разных видов. Обрабатываются
они по-разному; критерий — **область совпадения**.

**R1 — локальный дубль внутри одной декомпозиции → УДАЛЯЕМ.**
Сравнение идёт в пределах одного ответа модели:
- подзадача совпадает с названием самой разбираемой задачи
  (`is_parent_repeat`, порог 0.75) → строка удаляется;
- две подзадачи совпадают между собой (`is_duplicate`, порог 0.75) →
  остаётся первая по порядку, последующие удаляются.

Если после фильтрации список пуст (или осталась одна строка, равная
родителю) — **задача считается атомарной**, выполняется как есть
(Executor), дальнейшей декомпозиции не подлежит. Это и есть способ
получить `<atom>` без прямого токена от модели.

**R2 — глобальное совпадение с задачей ДРУГОЙ ветки дерева → НЕ удаляем,
создаём ссылку.** Сравнение идёт со всем деревом Kanban (нормализованный
`brief`, BKTree, высокий порог ~0.90):
- **оригиналом** становится самая ГЛУБОКАЯ задача (та, что появилась на
  последнем уровне декомпозиции): именно в ней реально возникла потребность,
  она содержит конкретику нижнего уровня;
- более мелкая совпадающая задача помечается ссылкой `duplicate_of=<id
  оригинала>` и НЕ исполняется отдельно;
- результат оригинала автоматически распространяется на все ссылки;
- родитель «мелкой» задачи получает блокировку по оригиналу: он завершится
  (summarize) только когда оригинал станет DONE;
- исходный порядок siblings сохраняется — ссылка не перемещается.

Итого: локальное повторение (R1) — сигнал «не делится», удаляется и даёт
атомарность; кросс-ветковое совпадение (R2) — сигнал «эта работа уже
планируется глубже», превращается в ссылку на глубокий оригинал. Ни в коем
случе R2 не приводит к удалению работы: она выполняется один раз — на
максимальной глубине.

Эталонная реализация (~20 строк):

```python
def parse_decomposition(response: str) -> List[Task]:
    tasks, current_brief, current_ctx = [], None, []
    for line in response.split("\n"):
        if line and not line[0].isspace():          # новая задача
            if current_brief:
                tasks.append(Task(brief=current_brief,
                                  description=" ".join(current_ctx)))
            current_brief, current_ctx = line.strip(), []
        elif line.strip() and current_brief:        # контекст
            current_ctx.append(line.strip())
    if current_brief:
        tasks.append(Task(brief=current_brief, description=" ".join(current_ctx)))
    return tasks
```

## 4. Модель данных (упрощённая)

```python
class TaskStatus(str, Enum):
    PENDING = "pending"; RUNNING = "running"
    DONE = "done";       FAILED = "failed"

@dataclass
class Task:
    id: str                       # new_id()
    brief: str                    # название (для RAG, дубль-детектора, дерева)
    description: str              # подробное описание
    status: TaskStatus = PENDING
    result: Optional[str] = None  # что получилось после выполнения
    parent_id: Optional[str] = None
    depth: int = 0
    subtasks: List[str] = field(default_factory=list)  # id детей (порядок = исполнение)
    duplicate_of: Optional[str] = None  # ссылка на оригинал (правило R2, §3.8)
```

Поле `duplicate_of` заполняется только CPU-слоем при глобальном совпадении
с задачей другой ветки; `None` = задача самостоятельна (оригинал или unique).
Ссылка (`duplicate_of != None`) не декомпозируется и не исполняется — её
статус и `result` зеркалятся с оригинала.

**УДАЛЕНО из v1** (определяется системой, а не моделью):
- `TaskCategory` (`>` / `!` / `?`) — категорий больше нет; порядок списка =
  порядок исполнения; зависимости определяет оркестратор;
- acceptance criteria;
- поле `blocked_by`.

`DecompositionCall` остаётся (task_id, task_brief, depth, think_enabled,
prompt, think, final_response, timings, error, atomic) — формат JSONL
истории совместим между v1/v2; `is_atomic` — свойство самого call.

## 5. Что видит модель (полный prompt одного вызова)

```
SYSTEM:
§2 (системный промпт, ~200 символов)

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
- think/no_think выбирается системой по глубине (L0–L2 think, L3+ no_think) —
  модель не управляет этим сама.

## 6. Роль CPU-слоя (модель этого не знает)

Перед декомпозицией (ContextEnricher, `enricher.py`):
1. RAG-поиск релевантной документации по `brief`;
2. SemanticMemory.recall(brief, top_k=3) — похожие решённые задачи;
3. Selective Context — сжатие длинных выводов siblings;
4. Чеклист выполненных siblings.

После декомпозиции (оркестратор):
1. `split_think_and_response()` → THINK / FINAL RESPONSE (логируются оба);
2. `is_atomic(final_response)` → атом или список;
3. `parse_decomposition()` → список подзадач;
4. DuplicateDetector, правило R1 (§3.8): локальные дубли — внутри списка и
   с родителем (порог 0.75) — удаляются; пустой список после фильтрации ⇒
   задача атомарна;
5. Глобальный дедуп по дереву Kanban, правило R2 (§3.8): совпадение с задачей
   другой ветки (нормализованный brief, BKTree, порог ~0.90) ⇒ оригиналом
   становится более глубокая задача, мелкая помечается `duplicate_of`;
6. Сохранение в SQLite Kanban; зависимости — по порядку; ссылки блокируют
   родителя до готовности оригинала.

После выполнения:
1. Сбор результатов детей; `summarize_results()` (конкатенация; опционально
   LLM-суммаризация) → `parent.result`;
2. SemanticMemory.remember(brief, result) для будущих recall.

При проблемах: ReflectAgent (`reflector.py`) анализирует провал,
ResearchAgent (`researcher.py`) изолированно собирает информацию.

## 7. Псевдокод ядра

```python
def execute_with_decomposition(task: Task) -> str:
    if task.status == DONE:
        return task.result
    if not task.subtasks:
        enriched = enricher.enrich(task.brief)
        call = decomposer.decompose(task, enriched)      # isolated HTTP call
        subs = parse(call.final_response)               # [] если <atom>/пусто
        subs = filter_local_dupes(subs, task)           # R1: порог 0.75
        if not subs:                                    # §3.8 R1: атом
            task.result = executor.execute(task)
            task.status = DONE
            return task.result
        for s in subs:                                  # R2: дедуп по дереву
            orig = kanban.find_duplicate(s.brief, threshold=0.90)
            if orig and orig.depth >= s.depth:          # оригинал — глубже
                s.duplicate_of = orig.id
            elif orig:                                  # новая глубже →
                kanban.repoint(orig, s)                 # она оригинал
        task.subtasks = subs
        kanban.save_all(children_of(task))
    results = []
    for i in task.subtasks:
        child = kanban.get(i)
        if child.duplicate_of:                          # ссылка ждёт оригинал
            results.append(execute_with_decomposition(kanban.get(child.duplicate_of)))
        else:
            results.append(execute_with_decomposition(child))
    task.result = summarize_results(results)
    task.status = DONE
    kanban.update(task)
    return task.result
```

## 8. Отличия v1 → v2 (кратко)

| Аспект | v1 (категории) | v2 (ультра-просто) |
|--------|----------------|--------------------|
| System prompt | ~2000 символов, категории | ~200 символов |
| Формат вывода | строки `> ! ?`, `<atom>` | brief + indented description, `<atom>` |
| Модель знает про | протокол категорий | только «задача + контекст» |
| Парсинг | category detection | 20 строк, отступы |
| Категории задач | RESEARCH/MUST_DO/DEFERRED | удалены (решает система) |
| Токенов на запрос | ~500 system | ~100 system |
| Модель данных | title + category + status-str | brief + description + TaskStatus |
| Дубликаты | только локальный фильтр | R1 локальный (удаление/атом) + R2 глобальный (ссылка `duplicate_of`, оригинал — глубже) |

## 9. Миграция кодовой базы (чеклист)

- [x] Документация v2 принята (этот файл, AGENTS.md §2.3–7/§10, architecture.md)
- [x] Правила дедупликации R1/R2 зафиксированы (§3.8), поле `Task.duplicate_of` (§4)
- [ ] `models.py`: `TaskStatus`, новый `Task(brief, description, ..., duplicate_of)`,
      удалить `TaskCategory`/`SYMBOL_TO_CATEGORY`; `DecompositionCall` под v2
- [ ] `parser.py`: `SYSTEM_PROMPT`, `ATOM_MARKER`, `is_atomic()`,
      `parse_decomposition()`, `build_user_prompt()`;
      удалить `detect_category`, `parse_response`, старую сборку prompt;
      `split_think_and_response`, `sanitize_line` — без изменений
- [ ] `detector.py`: API на `task.brief` вместо `task.title` (логика та же);
      два режима: R1 (локальный, порог 0.75) и R2 (глобальный, порог ~0.90)
- [ ] Тесты parser/detector переписать под v2-формат (+тесты на §3.8 R1/R2)
- [ ] history.py — по спецификации v2 (JSONL + debug.log, THINK/FINAL RESPONSE)
- [ ] Этапы 2–6 (client, cache_manager, decomposer/kanban/cli, bk_tree,
      semantic, rag, enricher/executor/reflector/researcher)
      — по AGENTS.md §6–7

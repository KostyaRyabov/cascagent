# cascagent — Модель данных и форматы

> Статус: **проект v2** (spec принят, код мигрируется).
> Документ детализирует Главы IV–V полной документации проекта
> (модель Task, ID-схема, история вызовов, research findings, enriched
> context) применительно к протоколу v2. Источник истины по формату
> общения с LLM — `docs/protocol.md`; по хранению — §3–4 этого файла.

## 1. Эволюция модели Task

**Версия 1 (отвергнута):** категории + сложные поля.

```python
@dataclass
class Task:
    id: str
    task: str
    category: TaskCategory        # > ! ? ⚛ — отвергнуто
    depth: int
    subtasks: List['Task']
    parent_id: Optional[str]
    acceptance: str               # отвергнуто — избыточно
    blocked_by: List[str]         # отвергнуто — определяется порядком
```

Причины отказа:
- категории усложняли промпт (~800 токенов вместо ~50);
- малая модель путалась в символах `> ! ?`;
- acceptance criteria модель генерировала плохо;
- `blocked_by` выводим из структуры дерева и порядка siblings.

**Версия 2 (финальная, зафиксирована в protocol §4):**

```python
class TaskStatus(str, Enum):
    PENDING = "pending"; RUNNING = "running"
    DONE = "done";       FAILED = "failed"

@dataclass
class Task:
    id: str                                    # dot-path: 0, 0.1, 0.1.2
    brief: str                                 # название одной строкой (~50 токенов, для RAG)
    description: Optional[str]                 # что нужно сделать (для LLM); None у атомов
    is_atom: bool = False                      # CPU-флаг: задача не декомпозируется
    embedding: Optional[bytes] = None          # float32 little-endian от embed(brief);
                                               # считается при инициализации задачи (плагин
                                               # TaskParser, plugins §7.4 / process §1.3)
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None               # что получилось после выполнения
    parent_id: Optional[str] = None
    depth: int = 0
    subtasks: List[str] = field(default_factory=list)  # id детей; порядок = исполнение
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
```

Почему эта модель оптимальна:
1. **`brief` и `description`** — две формы нужны по разным причинам:
   `brief` (~50 токенов) — индекс RAG/semantic-памяти, дубль-детектор, дерево;
   `description` (~200–400 токенов) — передаётся LLM для работы.
   Атомарная подзадача: `description = None`, `is_atom = True` (протокол
   допускает эквивалентную запись `description == ""` — нормализуется
   парсером к `None`, protocol §3).
2. **`is_atom` и `embedding`** — производные CPU-поля, модель их не задаёт:
   `is_atom` выставляется парсером (`<atom>` в описании / глобальный
   `<atom>`) и фильтром задач (process §1.3, правила F1–F3); `embedding`
   считается один раз при инициализации задачи из `brief` и переиспользуется
   recall/дубль-детектором (не пересчитывать на каждый поиск).
3. **Нет категорий** — логика «что делать с задачей» вынесена на CPU-слой;
   модель либо декомпозирует, либо пишет `<atom>`.
4. **Нет `blocked_by`** — зависимости определяются из структуры дерева и
   порядка siblings: если A идёт раньше B, B зависит от A (FIFO-планирование,
   cpu-offload A9).
5. **Нет `acceptance`** — критерии приёмки включаются в `description`;
   оркестратор проверяет покрытие результатом ребёнка (A7/A8).
6. **`subtasks` — список id, а не объектов** — сериализация тривиальна,
   объект задачи живёт только в Kanban (один источник истины).

Иерархия задач хранится в памяти проекта под системным ключом **`tasks`**
(project-уровень SessionMemory, plugins §5.7): словарь `{id: Task}` со
ссылками parent/children — единое живое рабочее дерево между плагином
TaskParser и оркестратором; в процессе декомпозиции оно уже заполнено, а
TaskParser подставляет новое поддерево детей к соответствующему родителю
(process §1.3, три стадии: парсинг → фильтрация → сохранение). Kanban
(SQLite) — персистентное зеркало того же дерева (resume после краша).

State machine статусов и инварианты — `docs/product.md` §5.

## 2. Идентификация задач

Схема ID — dot-separated path:

```
0            ← корень (depth 0)
├─ 0.1       ← подзадача уровня 1
│  ├─ 0.1.1
│  └─ 0.1.2
├─ 0.2
└─ 0.3
```

Преимущества: иерархия видна прямо в ID; родитель = отбросить последний
сегмент; siblings = общий `parent_id`; уникальность гарантирована структурой.

```python
def generate_subtask_id(parent_id: str, index: int) -> str:
    return f"{parent_id}.{index + 1}"

generate_subtask_id("0", 0)      # → "0.1"
generate_subtask_id("0.1", 1)    # → "0.1.2"
```

Инварианты: индекс monotonic per parent (проверяется перед вставкой —
конфликт = баг оркестратора, не молча переписываем); `depth ==
id.count('.')`; сегменты — положительные целые (CPU-валидация при загрузке
из БД).

## 3. История вызовов

### 3.1 DecompositionCall

Запись одного LLM-вызова (`history.py`). Два параллельных формата:
JSONL — машиночитаемый анализ/метрики/будущий файн-тюн; debug.log —
человекочитаемая отладка. Дублирование данных ~30% overhead — принято
осознанно; ротация debug.log по размеру.

```python
@dataclass
class DecompositionCall:
    timestamp: str
    task_id: str
    parent_task_id: Optional[str]
    task_brief: str                 # v2: brief вместо title
    prompt: str                     # полный SYSTEM+USER как отправлен
    think: Optional[str]            # извлечённый THINK (None если no_think)
    final_response: str             # FINAL RESPONSE после sanitize
    parsed: list[dict]              # [{"brief":..., "description":...|None, "is_atom":bool}] — v2
    error: Optional[str]            # тип/текст сбоя, если был
    duration_ms: int
    prompt_tokens: int              # из usage API (approx_tokens — fallback)
    completion_tokens: int
    think_enabled: bool
    duplicates_removed: int         # CPU-фильтр (detector)
    atomic: bool                    # свойство: is_atomic или degenerate-rule

    @property
    def is_atomic(self) -> bool: ...
```

Формат JSONL совместим между v1/v2: поле `category` убрано, `title` →
`task_brief`, добавлены `prompt_tokens/completion_tokens`. Строка записи:

```json
{"timestamp":"2026-10-09T14:23:41.123456","task_id":"0.1.2","parent_task_id":"0.1",
 "task_brief":"Создать модель User","prompt":"SYSTEM:\n...\n\nUSER:\n...",
 "think":"Нужно создать модель...","final_response":"Определить поля...\n    ...",
 "parsed":[{"brief":"Определить поля","description":"id (UUID), email..."}],
 "error":null,"duration_ms":12450,"prompt_tokens":410,"completion_tokens":850,
 "think_enabled":true,"duplicates_removed":2,"atomic":false}
```

debug.log — блоки с разделителями `--- THINK --- / --- FINAL RESPONSE --- /
--- PARSED (N tasks, atom=False) ---`, grep-able по task_id. Правила
разделения THINK/FINAL и degenerate-repeat — protocol §3, cpu-offload A1/A5.

### 3.2 Таблица llm_calls (SQL-зеркало)

Для агрегатных запросов (средняя длительность по глубине, доля атомов,
расход токенов) поверх JSONL — зеркало в SQLite. Запись идемпотентна
(`INSERT OR IGNORE` по AUTOINCREMENT id не гарантирует — пишем один раз
в транзакции сохранения вызова; JSONL остаётся primary, таблица — индекс).

## 4. Хранилища

### 4.1 Почему SQLite

| Альтернатива | Почему отвергнута |
|--------------|-------------------|
| PostgreSQL | Избыточно: отдельный сервер, credentials для локальной системы |
| Redis | In-memory, теряется при рестарте; нет транзакций для графа |
| JSON файлы | Нет атомарности/индексов; плохой concurrent access |
| LevelDB/RocksDB | Нет SQL — сложные запросы по графу громоздки |
| **SQLite** | ✅ один файл, ACID-транзакции, индексы, SQL, встроен в Python (stdlib) |

### 4.2 Схема БД (kanban.db)

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES tasks(id),
    depth INTEGER NOT NULL DEFAULT 0,
    brief TEXT NOT NULL,                    -- название (~50 токенов), для RAG/дерева
    description TEXT,                       -- для LLM; NULL у атомов (is_atom=1)
    is_atom INTEGER NOT NULL DEFAULT 0,     -- 0/1: задача не декомпозируется (CPU-флаг)
    embedding BLOB,                         -- float32 little-endian от embed(brief);
                                            -- считается при инициализации задачи
    status TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed
    result TEXT,                            -- итог выполнения
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    metadata TEXT                           -- JSON: расширения без миграций
);
CREATE INDEX idx_tasks_parent ON tasks(parent_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_depth  ON tasks(depth);

CREATE TABLE llm_calls (                    -- SQL-зеркало JSONL (§3.2)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id),
    timestamp TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    duration_ms INTEGER,
    think_enabled INTEGER,                  -- 0/1
    is_atom INTEGER,                        -- 0/1
    duplicates_removed INTEGER DEFAULT 0
);

CREATE TABLE research_cache (
    query_hash TEXT PRIMARY KEY,            -- sha256(normalize(query))
    query TEXT NOT NULL,
    findings TEXT NOT NULL,
    sources TEXT,                           -- JSON: список источников
    confidence REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE semantic_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_brief TEXT NOT NULL,
    task_result TEXT NOT NULL,
    embedding BLOB NOT NULL,                -- float32 little-endian, dim 384–768
    created_at TEXT NOT NULL
);
CREATE INDEX idx_semantic_created ON semantic_memory(created_at);
```

Конвенции: WAL-режим (`PRAGMA journal_mode=WAL`), `foreign_keys=ON`,
`check_same_thread=False` + внешний lock (single-flight оркестратора
достаточен); время — UTC ISO-8601 c timezone offset; boolean — INTEGER 0/1.

### 4.3 Ключевые операции Kanban

```python
class Kanban:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def add_task(self, task: Task) -> str: ...            # INSERT, возвращает id
    def update_status(self, task_id, status, result=None): ...
    def get_pending_tasks(self, depth: int | None = None) -> list[Task]: ...
    def get_siblings(self, task_id) -> list[Task]: ...     # тот же parent, по id-порядку
    def get_subtasks(self, task_id) -> list[Task]: ...
    def find_similar(self, embedding: bytes, top_k=3) -> list[Task]: ...
```

`find_similar`: SQLite не умеет cosine similarity нативно — эмбеддинги
читаем в numpy и считаем на CPU (cpu-offload P2); sqlite-vss/faiss —
опционально при росте базы (>10k записей).

### 4.4 Резервное копирование

SQLite — один файл. Опция `storage.backup_every_n_ops` (config §4.5):
периодический `sqlite3 conn.backup()` в `./data/backups/kanban-<ts>.db`;
ротация — последние N копий. History/debug.log append-only, бэкап не нужен.

## 5. ResearchFinding

```python
@dataclass
class ResearchFinding:
    query: str                    # что искали (normalized — ключ кэша)
    findings: str                 # что нашли (уже сжатием ≤ budget)
    sources: list[str]            # откуда (URL / путь документа)
    confidence: float             # 0.0–1.0 (оценивает сам ResearchAgent)
    created_at: str

    def to_context_block(self) -> str:
        src = ", ".join(self.sources[:3])
        return f"[Исследование: {self.query}]\n{self.findings}\n(источники: {src})"
```

Кэш: ключ `sha256(normalize(query))` (та же нормализация, что exact-match
кэш решений, cpu-offload P1) → повтор исследования = 0 LLM-вызовов;
hit по похожей формулировке — через semantic recall перед вызовом агента.
ResearchAgent — изолированный вызов с собственным коротким system
промптом (product §4); findings попадают в контекст только через
EnrichedContext, модель-декомposer не знает об источнике.

## 6. EnrichedContext

```python
@dataclass
class EnrichedContext:
    rag_documents: list[str] = []          # из документации (RAG)
    semantic_memories: list[str] = []      # из опыта (recall)
    similar_tasks: list[str] = []          # из Kanban
    research_findings: list[ResearchFinding] = []
    completed_siblings: list[dict] = []    # {"brief":..., "result":...}
    total_tokens: int = 0
    confidence: float = 0.0
    gaps: list[str] = []                   # чего не хватает (сигнал оркестратору)

    def to_prompt_block(self, max_tokens: int = 500) -> str: ...
```

Принципы сборки (pure function + CPU-библиотеки, cpu-offload P1–P6):
1. enricher собирает источники, форматирует блоками (`Документация:`,
   `Похожий опыт:`, `Выполненные ранее: ✓`, `Результаты исследований:`);
2. превышение бюджета → Selective Context сжатие (P5), затем — обрезка
   по приоритету: siblings > research > semantic > RAG;
3. `total_tokens` считается до отправки (учёт бюджета ctx, stack §6);
4. **модель не знает**, откуда информация — она видит блок «Контекст».

## 7. Связи между сущностями

```
Task ──1:N──> Task (subtasks/parent_id, dot-path)
 │  ├─ 1:N ──> DecompositionCall (history JSONL + llm_calls)
 │  ├─ N:M ──> ResearchFinding (через research_cache по запросу задачи)
 │  └─ 1:1 ──> semantic_memory (remember(brief,result) после DONE)
EnrichedContext — собирается на лету из Kanban + semantic + RAG + cache
```

Навигация: `get_subtasks / get_task(parent_id) / get_siblings`;
обогащение берёт только `[s for s in siblings if s.status == DONE]`.

## 8. Открытые вопросы

- Файн-тюнинг из history.jsonl (упомянут в Главе V оригинала) — вне
  скоупа v0.x; формат записи специально сохраняет полный prompt/raw ответ.
- Порог перехода `find_similar` с numpy-перебора на faiss/sqlite-vss.
- Схема `metadata` JSON — свободная до появления первых реальных расширений.

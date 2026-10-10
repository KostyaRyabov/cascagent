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

**Версия 2 (актуальная, зафиксирована в protocol §4):**

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional

class TaskStatus(str, Enum):
    PENDING = "pending"       # задача ещё НЕ начала работу — ждёт очереди в
                              # FIFO; над ней работают только CPU-инварианты
                              # декомпозиции (F1–F3, эмбеддинги)
    ENRICHMENT = "enrichment" # работа началась, но LLM-вызова ещё нет: этап
                              # подготовки — выбор агента-исполнителя, подбор
                              # MCP-инструментов, запросы в базы знаний/recall,
                              # формирование контекста (process §1.1)
    RUNNING = "running"       # контекст собран, задача выполняется — активный
                              # LLM-вызов (декомпозиция или исполнение атома)
    DONE = "done"             # задача корректно выполнена
    FAILED = "failed"         # ошибка либо недостаточно данных для выполнения

@dataclass
class Task:
    id: int                                    # snowflake (data.md §2): кодирован created_at;
                                               # генерируется В КОНСТРУКТОРЕ задачи —
                                               # экземпляр Task без id невозможен
    brief: str                                 # название одной строкой (~50 токенов, для RAG)
    description: Optional[str]                 # что нужно сделать (для LLM); None у атомов
    is_atom: bool = False                      # CPU-флаг: задача не декомпозируется
                                               # (<atom> в описании / глобальный <atom>
                                               # либо правила F1/F3 — process §1.3)
    embedding: Optional[List[float]] = None    # вектор от embed(brief); считается один раз
                                               # при инициализации задачи (плагин TaskParser,
                                               # plugins §7.4 / process §1.3); отдельный
                                               # embedding(description) НЕ хранится (см. п.2)
    canonical: Optional['Task'] = None         # v2+ (roadmap #10): прямая ссылка на задачу-
                                               # оригинал при семантическом дубле; в v1 None
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None               # что получилось после выполнения
    parent: Optional['Task'] = None            # прямая ссылка на родителя; в живом дереве
                                               # (project-ключ `tasks`) все ссылки — объекты
    order: int = 0                             # позиция среди siblings (порядок задаёт
                                               # парсер; в SQL — колонка `order`;
                                               # исполнение — по возрастанию внутри parent)
    depth: int = 0                             # уровень детализации: depth(child) =
                                               # depth(parent) + 1 (инкремент от родителя)
    subtasks: List['Task'] = field(default_factory=list)  # дети; порядок = исполнение;
                                               # default_factory=list — mutable default:
                                               # каждый экземпляр получает СВОЙ новый []
                                               # (можно append без предварительной
                                               # инициализации поля)
    created_at: datetime                       # обязателен (без default — заполняется при
                                               # создании Task; datetime.now(timezone.utc);
                                               # None недопустим: из него кодируется snowflake id)
    started_at: Optional[datetime] = None      # проставляется оркестратором при переходе
                                               # PENDING → ENRICHMENT (момент начала работы,
                                               # до LLM-вызова; RUNNING — уже с собранным
                                               # контекстом)
    finished_at: Optional[datetime] = None     # проставляется при DONE / FAILED
```

> **Реализация:** типизация выше — идеалистическая (прямые ссылки `Task`,
> `datetime`). SQLAlchemy-модель kanban (§4.2) хранит те же сущности в
> сериализуемом виде: `parent_id`/`canonical_id` INTEGER (snowflake id), связи
> через `relationship()` восстанавливают ссылки при загрузке; время —
> TIMESTAMP (UTC ISO-8601 в TEXT-колонках legacy-дампов); `embedding` —
> BLOB float32 little-endian (wire-формат, не отдельная модель).

Почему эта модель оптимальна:
1. **`brief` и `description`** — две формы нужны по разным причинам:
   `brief` (~50 токенов) — индекс RAG/semantic-памяти, дубль-детектор, дерево;
   `description` (~200–400 токенов) — передаётся LLM для работы.
   Атомарная подзадача: `description = None`, `is_atom = True` (протокол
   допускает эквивалентную запись `description == ""` — нормализуется
   парсером к `None`, protocol §3).
2. **`is_atom`, `embedding` и `canonical`** — производные CPU-поля, LLM
   их не задаёт:
   `is_atom` выставляется парсером (`<atom>` в описании / глобальный
   `<atom>`) и стадией фильтрации плагина TaskParser (process §1.3, правила
   F1–F3); `embedding` считается один раз при инициализации задачи из `brief`
   и переиспользуется recall/дубль-детектором (не пересчитывать на каждый
   поиск). **Отдельный `embedding(description)` не хранится**: он нужен был
   бы только для проверки «brief ≈ description ⇒ атом» (F1) — это сравнение
   выполняется на лету внутри TaskParser (стадия 2), результат фиксируется
   флагом `is_atom`, а вектор описания дальше нигде не пригождается;
   `canonical` — поле будущего графа задач (roadmap #10), в v1 остаётся `None`.
3. **Нет категорий** — логика «что делать с задачей» вынесена на CPU-слой;
   модель либо декомпозирует, либо пишет `<atom>`.
4. **Нет `blocked_by`** — зависимости определяются из структуры дерева и
   порядка siblings: если A идёт раньше B, B зависит от A (FIFO-планирование,
   cpu-offload A9).
5. **Нет `acceptance`** — критерии приёмки включаются в `description`;
   оркестратор проверяет покрытие результатом ребёнка (A7/A8).
6. **В памяти — прямые ссылки, в БД — id.** В живом дереве `parent`,
   `subtasks` и `canonical` — объекты `Task` (никаких `kanban.get(id)` на
   каждый шаг обхода). Сериализация тривиальна: snowflake id уже хранится в
   самом поле `id` задачи, поэтому SQL-строка (§4.2) выводится из объекта без
   отдельного индекса-словаря; при загрузке из Kanban связи восстанавливаются
   по `parent_id` за один проход.
7. **Иерархия — не в id.** Snowflake id непрозрачен для дерева (в отличие от
   прежней dot-path схемы): родство держится на ссылках `parent`/`subtasks`
   (+ `parent_id` в SQL), порядок siblings — на поле `order`, глубина — на
   `depth`. Обход «дерево вниз» = индексация `subtasks`, «вверх» = цепочка
   `parent`; сортировка исполнения — `ORDER BY parent_id, "order"` (§4.2).

Иерархия задач хранится в памяти проекта под системным ключом **`tasks`**
(project-уровень SessionMemory, plugins §5.7): словарь `{id: Task}` со
ссылками parent/children — единое живое рабочее дерево между плагином
TaskParser и оркестратором; в процессе декомпозиции оно уже заполнено, а
TaskParser подставляет новое поддерево детей к соответствующему родителю
(process §1.3, три стадии: парсинг → фильтрация → сохранение). Kanban
(SQLite) — персистентное зеркало того же дерева (resume после краша).

State machine статусов и инварианты — `docs/product.md` §5.

## 2. Идентификация задач — snowflake

ID задачи — **snowflake** (алгоритм генерации уникальных ID, Twitter):
монотонный 64-битный целый, в который закодирован момент создания
(`created_at`). Формируется **в конструкторе Task** (`__post_init__`: сначала
фиксируется `created_at = datetime.now(timezone.utc)`, затем
`id = snowflake_ids.next_id(created_at)`; TaskParser, process §1.3), поэтому
id существует с первой секунды жизни задачи и содержит дату внутри — по нему
можно восстановить время создания без отдельного lookup.

Битовая компоновка (как в Twitter Snowflake, знаковый int64):

```
┌──────────────────────────────────────────────────────────┐
│ 0 (1 бит) │ Timestamp (41 бит) │ Machine (10) │ Seq (12) │
└──────────────────────────────────────────────────────────┘
   unused      ms от эпохи проекта   node_id      счётчик в пределах мс
```

- **Timestamp (41 бит)** — миллисекунды с эпохи проекта (запас ~69 лет);
- **Machine ID (10 бит)** — идентификатор ноды/процесса (до 1024), из env/config;
- **Sequence (12 бит)** — счётчик в рамках миллисекунды (до 4096 id/ms).

Эпоха проекта: `EPOCH = 2026-01-01T00:00:00Z` (фиксирована в конфиге,
config §4 `[ids] epoch`; менять нельзя — это часть контракта id).

```python
import threading
from datetime import datetime, timezone

class SnowflakeGenerator:
    """Потокобезопасный генератор snowflake id одного процесса."""
    EPOCH_MS  = 1_767_225_600_000   # 2026-01-01T00:00:00Z, ms
    NODE_BITS = 10                  # до 1024 процессов (node_id из env/config)
    SEQ_BITS  = 12                  # до 4096 id на одну миллисекунду

    def __init__(self, node_id: int):
        assert 0 <= node_id < (1 << self.NODE_BITS)
        self.node_id, self._seq, self._last_ms = node_id, 0, -1
        self._lock = threading.Lock()

    def next_id(self, ts: datetime) -> int:
        ms = int(ts.timestamp() * 1000) - self.EPOCH_MS
        with self._lock:
            if ms == self._last_ms:
                self._seq = (self._seq + 1) & ((1 << self.SEQ_BITS) - 1)
                if self._seq == 0:                 # переполнение счётчика в этой мс
                    while ms == self._last_ms:     # ожидаем следующую миллисекунду
                        ms = int(datetime.now(timezone.utc).timestamp() * 1000) - self.EPOCH_MS
            else:
                self._seq = 0
            self._last_ms = ms
        return (ms << 22) | (self.node_id << 12) | self._seq

# обратное преобразование (для отладки/логирования):
def created_at_from_id(task_id: int) -> datetime:
    ms = (task_id >> 22) + SnowflakeGenerator.EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
```

Свойства схемы:
- **Монотонность**: id возрастают вместе со временем ⇒ «новые задачи раньше
  старых» проверяется простым сравнением чисел (исторично заменяет прежнюю
  FIFO-сортировку по dot-path);
- **Информативность**: `created_at` восстановим из id (§ выше) — дедупликация
  истории и разбор логов не требуют JOIN с таблицей задач;
- **Коллизии исключены** в пределах узла (sequence + ожидание мс); при запуске
  нескольких процессов оркестратора каждому выдаётся уникальный `node_id`;
- **Компактность**: int64 — одно целое вместо строкового id, без обращения к
  внешнему сервису генерации (децентрализованно, только локальный lock).

В модели Task это реализуется так (dataclass, idealized §1):

```python
@dataclass
class Task:
    id: int = field(default_factory=lambda: snowflake_ids.next_id())
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # ... остальные поля

    def __post_init__(self):
        # id кодирует именно этот created_at (один источник времени)
        self.id = snowflake_ids.next_id(self.created_at)
```

Что заменили (dot-path `0`, `0.1`, `0.1.2`): родство теперь только через
`parent`/`subtasks` (+ `parent_id` в SQL), порядок siblings — через поле
`order` (проставляет парсер по позиции блока в ответе LLM), глубина — через
`depth = parent.depth + 1`. Инварианты загрузки из БД: у каждой задачи
(кроме корневых) есть существующий `parent_id`; `"order"` уникален внутри
`(parent_id, order)`; `depth` согласован с длиной цепочки parent (проверка —
CPU-валидация, cpu-offload A13).

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
    task_id: int                    # snowflake (data.md §2)
    parent_task_id: Optional[int]
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
{"timestamp":"2026-10-09T14:23:41.123456","task_id":7,"parent_task_id":6,
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
    id INTEGER PRIMARY KEY,                 -- snowflake (data.md §2): кодирован created_at
    parent_id INTEGER REFERENCES tasks(id), -- NULL у корневых задач
    "order" INTEGER NOT NULL DEFAULT 0,     -- позиция среди siblings (порядок = исполнение)
    depth INTEGER NOT NULL DEFAULT 0,       -- = depth(parent) + 1
    brief TEXT NOT NULL,                    -- название (~50 токенов), для RAG/дерева
    description TEXT,                       -- для LLM; NULL у атомов (is_atom=1)
    is_atom INTEGER NOT NULL DEFAULT 0,     -- 0/1: задача не декомпозируется (CPU-флаг)
    embedding BLOB,                         -- float32 little-endian от embed(brief);
                                            -- считается при инициализации задачи
    status TEXT NOT NULL DEFAULT 'pending', -- pending|enrichment|running|done|failed
                                            -- (pending = работа не начата; enrichment —
                                            -- старт: сбор контекста/ресурсов до LLM-вызова)
    result TEXT,                            -- итог выполнения
    created_at TIMESTAMP NOT NULL,          -- datetime UTC (в Python-модели — datetime, data.md §1);
                                            -- из него кодируется snowflake id (§2)
    started_at TIMESTAMP,                   -- проставляется при PENDING → ENRICHMENT
    finished_at TIMESTAMP,                  -- проставляется при DONE / FAILED
    metadata TEXT                           -- JSON: расширения без миграций
);
CREATE INDEX idx_tasks_parent ON tasks(parent_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_depth  ON tasks(depth);
CREATE UNIQUE INDEX idx_tasks_parent_order ON tasks(parent_id, "order");

CREATE TABLE llm_calls (                    -- SQL-зеркало JSONL (§3.2)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
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
достаточен); время — TIMESTAMP, хранится как UTC ISO-8601 c timezone
offset, при загрузке конвертируется в `datetime` (Python-модель оперирует
только `datetime`, строки не хранит); boolean — INTEGER 0/1.

### 4.3 Ключевые операции Kanban

```python
class Kanban:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def add_task(self, task: Task) -> int: ...            # INSERT (id — snowflake), возвращает id
    def update_status(self, task_id, status, result=None): ...
    def get_pending_tasks(self, depth: int | None = None) -> list[Task]: ...
    def get_siblings(self, task_id) -> list[Task]: ...     # тот же parent, по ("order", id)
    def get_subtasks(self, task_id) -> list[Task]: ...     # дети, по ("order", id)
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
Task ──1:N──> Task (subtasks/parent_id + "order"; id — snowflake, §2)
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

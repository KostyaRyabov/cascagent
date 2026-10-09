# cascagent — Референсные CPU-алгоритмы (эталонные реализации)

> Реестр «что делает CPU и чего НЕ делает LLM» — `docs/cpu-offload.md`
> (таблицы P1–P9 / A1–A13). Этот документ — **референсные реализации**
> ключевых алгоритмов из Главы VII полной документации проекта: здесь живут
> полные кодовые листинги, оценки сложности и договорённости по параметрам.
> При расхождении: протокол задач — `protocol.md`, распределение шагов по
> слоям — `cpu-offload.md`, код при конфликте с этим документом — баг.

## 0. Общие правила для всех алгоритмов слоя

1. Детерминизм: никаких обращений к LLM внутри этих модулей; тестируются без
   сервера (`pytest -m "not llm"`).
2. Тяжёлые зависимости (`Levenshtein`, `sentence-transformers`, `rank-bm25`,
   `faiss`) — только в extras (`config.md §3`); каждый модуль обязан иметь
   чистый stdlib fallback, чтобы ядро Этапов 1–3 работало без extras:
   - BK-tree → собственный DP-Левенштейн (~15 строк);
   - semantic/RAG → опциональность через ImportError + no-op режим.
3. Все пороги (`threshold`, `max_distance`, `top_k`, `min_similarity`) —
   из `config.toml` (секции `[decomposer]`, `[enrichment]`), не хардкод.
4. Вход любой строки сначала проходит `normalize()` (§6.1) — регистр,
   схлопнутые пробелы; это общий канон сравнений для всего слоя.
5. Если алгоритм уже реализован в `src/cascagent/` (detector, parser),
   эталоном считается **код + его тесты**, а не листинг из Главы VII полной
   документации; здесь фиксируем только API-контракт и недостающие модули.

## 1. BK-tree — fuzzy-поиск по метрическому пространству (`bk_tree.py`)

**Задача:** агент написал `read_fiel`; найти правильный инструмент из ~1000
кандидатов за O(log n) вместо перебора. Применяется для: выбора MCP-инструмента
(P4), исправления опечаток имён, поиска похожих задач/файлов по имени.

```python
from typing import Callable, Optional


class BKTree:
    """Burkhard–Keller tree. Узел = (слово, {расстояние_до_родителя: поддерево}).
    Поиск отсекает ветви, где расстояние не может попасть в [d-m, d+m]."""

    def __init__(self, distance_fn: Callable[[str, str], int] = None):
        self.distance_fn = distance_fn or levenshtein_distance
        self.tree: Optional[tuple[str, dict]] = None

    def add(self, word: str) -> None:
        if self.tree is None:
            self.tree = (word, {})
            return
        node, children = self.tree
        dist = self.distance_fn(word, node)
        while dist in children:                      # спуск до свободного слота
            node, children = children[dist]
            dist = self.distance_fn(word, node)
        children[dist] = (word, {})

    def query(self, word: str, max_distance: int = 2) -> list[tuple[str, int]]:
        results: list[tuple[str, int]] = []
        if self.tree is not None:
            self._search(self.tree, word, max_distance, results)
        results.sort(key=lambda x: x[1])             # ближайший — первый
        return results

    def _search(self, node, word, max_distance, results):
        current, children = node
        dist = self.distance_fn(word, current)
        if dist <= max_distance:
            results.append((current, dist))
        # треугольное неравенство: дети возможны только в диапазоне
        for d in range(dist - max_distance, dist + max_distance + 1):
            if d in children:
                self._search(children[d], word, max_distance, results)

    def find_best(self, word: str, max_distance: int = 2) -> Optional[str]:
        r = self.query(word, max_distance)
        return r[0][0] if r else None
```

**Метрика корректности отсечения** — неравенство треугольника: если
`d(q,node)=D`, то любой потомок с расстоянием до узла `c` лежит в
`[D−c, D+c]`, поэтому ветви вне `[D−m, D+m]` просматривать бессмысленно.

| Вариант | n=1000 инструментов | Комментарий |
|---|---|---|
| Наивный перебор + Левенштейн | ~50 мс | O(n·len²) |
| BK-tree, max_distance=2 | ~0.5 мс | ≈100× быстрее; деградация O(n) только при плохом дереве |

## 2. Левенштейн и фонетика (`fuzzy.py`)

Расстояние редактирования (`python-Levenshtein`, либо DP-fallback) плюс
фонетическая нормализация для случаев, когда правки много, а звучание то же
(`Katherine/Catherine`, русские безударные гласные, оглушение):

```python
class PhoneticMatcher:
    RU = {"о": "а", "е": "и", "я": "и", "ю": "у",
          "г": "к", "д": "т", "б": "п", "в": "ф", "з": "с", "ж": "ш"}

    def russian_phonetic(self, word: str) -> str:
        w = word.lower()
        return "".join(self.RU.get(ch, ch) for ch in w)
    # для английских имён/терминов — soundex/metaphone (stdlib-реализации нет,
    # в v0.x используем только как третий уровень fallback)
```

**FuzzyMatcher — конвейер уровней** (первые сработавшие уровни дешевле;
порядок фиксирован, эскалация только при пропуске предыдущего):

```python
class FuzzyMatcher:
    def __init__(self, candidates: list[str]):
        self.candidates = candidates
        self.index = set(candidates)                 # exact — O(1)
        self.bk = BKTree()
        for c in candidates:
            self.bk.add(c)

    def find(self, query: str) -> str | None:
        if query in self.index:                      # 1. точное совпадение
            return query
        best = self.bk.find_best(query, max_distance=1)   # 2. близкая опечатка
        if best:
            return best
        best = self.bk.find_best(query, max_distance=2)   # 3. дальняя опечатка
        if best:
            return best
        ph = PhoneticMatcher().russian_phonetic(query)     # 4. фонетика
        for c in self.candidates:
            if PhoneticMatcher().russian_phonetic(c) == ph:
                return c
        return None                                  # нет кандидата — ошибка CPU-слоя,
                                                     # НЕ ретрай у LLM
```

Примеры базовых операций: `distance("auth","oauth")==1`;
`ratio("hello","helo")≈0.89`.

## 3. SemanticMemory — долгосрочная память опыта (`semantic.py`)

Хранение пар (задача → результат) как эмбеддингов; поиск cosine similarity.
Схема таблицы — `data.md §4` (`semantic_memory`); здесь — поведение.

- `remember(brief, result, source_task_id)` — после успешного DONE (A12);
  эмбеддинг `brief` (не description — короче и стабильнее как ключ опыта).
- `recall(query, top_k=3, min_similarity=0.7)` — перед вызовом модели (P2);
  результаты кладутся в `EnrichedContext.semantic_memories`.
- Реализация v0.x: numpy-перебор BLOB'ов (~50 мс на 1000 записей, CPU-only
  all-MiniLM-L6-v2 encode ≈10 мс/строка). Порог миграции на faiss-cpu /
  sqlite-vss — >10k записей (открытый вопрос data.md §9).
- `forget_old(days=30, max_keep=1000)` — GC от разрастания: сначала по
  возрасту, затем trim по новизне до `max_keep`.

```python
def recall(self, query: str, top_k: int = 3, min_similarity: float = 0.7):
    q = self.encoder.encode(query).astype("float32")
    scored = []
    for brief, result, blob in self.db.execute(
            "SELECT task_brief, task_result, embedding FROM memories"):
        m = np.frombuffer(blob, dtype="float32")
        sim = float(q @ m / (np.linalg.norm(q) * np.linalg.norm(m)))
        if sim >= min_similarity:
            scored.append((sim, brief, result))
    scored.sort(reverse=True)
    return [{"task_brief": b, "task_result": r, "similarity": s}
            for s, b, r in scored[:top_k]]
```

Важно: память **обогащает** контекст, но никогда не заменяет решение модели
о декомпозиции (hit semantic ≠ авто-ответ, кроме exact-match кэша P1).

## 4. RAG двухступенчатый (`rag.py`)

BM25 (ключевые слова, дёшево) → vector rerank top-50 (семантика, точнее).
Документация индексируется при старте; запрос — из обогащения (P3).

```python
def search(self, query: str, top_k: int = 3):
    scores = self.bm25.get_scores(self._tokenize(query))
    cand = np.argsort(scores)[::-1][:50]                 # ступень 1: BM25
    if len(cand) <= top_k:
        return [self.documents[i] for i in cand]
    qv = self.encoder.encode([query])[0]                 # ступень 2: vectors
    sims = self.doc_embeddings[cand] @ qv / (
        np.linalg.norm(self.doc_embeddings[cand], axis=1) * np.linalg.norm(qv))
    pick = cand[np.argsort(sims)[::-1][:top_k]]
    return [{**self.documents[i], "relevance": float(s)}
            for i, s in zip(pick, np.sort(sims)[::-1])]
```

Токенизация — `re.findall(r"\w+", text.lower())` (unicode `\w` держит кириллицу).
Fallback без extras: чистый FTS5-индекс SQLite (BM25 встроен в SQLite) — тогда
vector-ступень отсутствует, качество ниже, но режим работает.

## 5. RobustYAMLParser — толерантный разбор «почти YAML» (кандидат в `parser.py`)

Статус: **зарегистрирован, не реализован** (вывод декомпозиции парсится
отступами, не YAML — prompts §4). Нужен для внешних форматов: манифесты
MCP-инструментов, настройки config.toml-редакторов, ответы сторонних агентов.
Порог реализации — первый этап, где появляется импорт чужих YAML'ов. Стратегии применяются по нарастанию
агрессивности, первая успешная выигрывает:

1. прямой `yaml.safe_load`;
2. извлечение блока ` ```yaml … ``` `;
3. фикс кавычек (умные “ ” ‘ ’ → прямые, склейка `"""`→`""`);
4. нормализация отступов (табы→4 пробела, округление кратным 2);
5. проставление недостающих двоетий у «ключевидных» строк;
6. regex-extraction `^\s*([\w\-]+)\s*:\s*(.+)$` с выводом типов (bool/int/str).

Правило: вернули `None` — это **валидационная ошибка уровня A**, идёт
ретрай с подсказкой формата (cpu-offload §2.3), молчаливый partial-parse
запрещён (лучше явно упасть, чем взять половину манифеста).

## 6. DuplicateDetector — защита от зацикливания (`detector.py`)

Эталон API — `src/cascagent/detector.py::DuplicateDetector` (реализован;
порог `similarity_threshold` из config, дефолт 0.75). Контракт методов:

```python
class DuplicateDetector:
    def __init__(self, threshold: float = 0.75):
        self.threshold = threshold

    def similarity(self, a: str, b: str) -> float:      # normalize + SequenceMatcher
        a, b = self._norm(a), self._norm(b)
        return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()

    def is_duplicate(self, new: str, existing: list[str]) -> bool: ...
    def is_parent_repeat(self, task: str, parent: str) -> bool: ...
    def detect_cycle(self, task: str, ancestors: list[str]) -> bool: ...
    def find_duplicates(self, tasks: list[str]) -> list[tuple[int, int, float]]: ...
```

### 6.1. normalize() — общий канон сравнения

`lower()` + схлопывание повторных пробелов в один. Только это; удаление
стоп-слов/лемматизация в v0.x сознательно **не** делаем — непредсказуемо для
кода и имён файлов в brief.

### 6.2. Маппинг методов на реестр cpu-offload

| Метод | Используется в | Эффект |
|---|---|---|
| `is_parent_repeat` | A5 degenerate-repeat | 1 подзадача ≈ родитель → трактуем как `<atom>` |
| `is_duplicate` / `find_duplicates` | A6 дедуп siblings | удаляем повторы, пишем `duplicates_removed` в history |
| `detect_cycle` | A7 антицикл по предкам | задача похожа на любого предка до корня → отклонить ветку |

Сложность: `similarity` O(len²) худш., `find_duplicates` O(k²·len) при k≤8
детях — наносекунды относительно GPU-вызова; threshold 0.75 таргирует
«перефразированный дубль», <0.6 ложных слияний не ожидается (калибруется по
history.jsonl, метрика atom_task_ratio — performance §5).

## 7. Тестодоступность и метрики

Каждый алгоритм §1–§6 имеет юнит-тесты без LLM (fixture-корпус из
`tests/fixtures/tools.txt`, `docs_sample/`). Что считать после запуска
сессии — сборщик метрик `PerformanceMetrics` описан в `performance.md §5`;
сырьё — `history.jsonl` (DecompositionCall, data.md §3).

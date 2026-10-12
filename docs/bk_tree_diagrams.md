# BK-tree — схемы (Mermaid)

Визуальное дополнение к документации BK-tree: архитектура, структура дерева,
жизненный цикл операций, метрики, статистика и ребалансировка.
Реализация: `src/cascagent/bk_tree.py`, референс: `docs/algorithms.md §1`.

---

## 1. Архитектура модуля (классы и зависимости)

```mermaid
classDiagram
    class DistanceMetric {
        <<abstract>>
        +compute(a, b) int~float~
        -_round_distance(d) int~float~
    }
    class LevenshteinDistance {
        +compute(a, b) int
    }
    class SemanticDistance {
        -embedding_fn Callable
        -cache Dict~str, ndarray~
        -precision int
        +compute(a, b) float
        +save_cache(path)
        +load_cache(path)
    }
    class HammingDistance {
        +compute(a, b) int
    }
    class JaccardDistance {
        -precision int
        +compute(a, b) float
    }

    class BKTreeConfig {
        <<dataclass>>
        +enable_analytics bool
        +rebalance_check_interval int
        +rebalance_quality_threshold float
        +auto_rebalance bool
        +on_need_rebalance Callable
    }
    class BKTreeStats {
        -read_weights Counter
        -depths Dict~str_int~
        -needs_rebalance bool
        -last_quality_score float
        +set_depth(word, depth)
        +record_read(word)
        +get_sorted_by_weight() List~str~
        +get_quality_score() float
        +reset()
    }
    class BKNode {
        <<dataclass>>
        +word str
        +children Dict~dist_BKNode~
    }
    class BKTree {
        -root BKNode
        -size int
        -_all_words List~str~
        -_lock Lock
        +add(word)
        +append(words)
        +query(word, max_distance) List
        +contains(word) bool
        +rebalance()
        +needs_rebalance() bool
        +get_stats() BKTreeStats
        +clear_stats()
    }

    DistanceMetric <|-- LevenshteinDistance
    DistanceMetric <|-- SemanticDistance
    DistanceMetric <|-- HammingDistance
    DistanceMetric <|-- JaccardDistance

    BKTree --> DistanceMetric : использует
    BKTree --> BKTreeConfig : конфигурация
    BKTree --> BKTreeStats : аналитика (опц.)
    BKTree o-- BKNode : root / children
    BKTreeConfig ..> BKTree : on_need_rebalance(tree, score)
```

**Ключевые принципы:** метрика — отдельная сущность (не часть конфига);
статистика минимальна (счётчик веса + глубина); глубина проставляется один раз
при добавлении; ребалансировка — полная пересборка.

---

## 2. Структура дерева (пример: Левенштейн)

Слова: `["read_file", "write_file", "delete_file", "list_directory"]`.
Корень — первое добавленное слово; рёбра подписаны расстоянием до родителя.

```mermaid
graph TD
    R["read_file<br/>(depth 0, корень)"]
    W["write_file<br/>(depth 1)"]
    D["delete_file<br/>(depth 1)"]
    L["list_directory<br/>(depth 1)"]

    R -- "d = 3" --> W
    R -- "d = 6" --> D
    R -- "d = 10" --> L

    style R fill:#4a90d9,color:#fff
    style W fill:#7cb8e6
    style D fill:#7cb8e6
    style L fill:#7cb8e6
```

Инвариант BK-tree: **все дети узла имеют уникальные расстояния до него**.
Поэтому при поиске достаточно обойти только детей с расстоянием `d`
в диапазоне `[dist(query, node) − max_distance, dist(query, node) + max_distance]`
(неравенство треугольника).

---

## 3. Добавление слова (`add` / `append`)

```mermaid
flowchart TD
    A(["add(word)"]) --> B{"Дерево пусто?<br/>root is None"}
    B -- да --> C["root = BKNode(word)<br/>stats.set_depth(word, 0)"]
    C --> Z([готово])
    B -- нет --> D["node = root, depth = 0"]
    D --> E["dist = metric.compute(word, node.word)<br/>depth += 1"]
    E --> F{"dist == 0?"}
    F -- да --> G["Слово уже есть — пропуск"]
    G --> Z
    F -- нет --> H{"dist в node.children?"}
    H -- да --> I["node = children[dist]"] --> E
    H -- нет --> J["children[dist] = BKNode(word)<br/>size += 1, _all_words.append(word)<br/>stats.set_depth(word, depth)"]
    J --> Z
```

Операция под `threading.Lock()` — потокобезопасна.

---

## 4. Поиск (`query`) с отсечением ветвей

Пример: `query("read_fiel", max_distance=2)` (опечатка, d(read_fiel, read_file)=2).

```mermaid
flowchart TD
    A(["query(word, max_distance)"]) --> B{"root is None?"}
    B -- да --> C[("возврат [ ]")]
    B -- нет --> D["Вычислить d = dist(word, node.word)"]
    D --> E{"d ≤ max_distance?"}
    E -- да --> F["добавить (node.word, d) в results"]
    E -- нет --> G
    F --> G["Обойти детей с ключом k ∈<br/>[d − max_dist, d + max_dist]<br/>(неравенство треугольника)"]
    G --> H{"Есть ещё дети<br/>в диапазоне?"}
    H -- да --> D2["рекурсия в поддерево"] --> D
    H -- нет --> I["Подъём по стеку рекурсии"]
    I --> J{"Вернулись в корень?"}
    J -- нет --> G
    J -- да --> K["results.sort по расстоянию ↑"]
    K --> L["analytics: record_read для каждого найденного<br/>_queries_since_check += 1"]
    L --> M["_maybe_check_rebalance()"]
    M --> N([результаты: список word, distance])

    style A fill:#4a90d9,color:#fff
    style N fill:#5cb85c,color:#fff
```

Результат для примера выше: `[("read_file", 2)]`. Сложность O(log n)
в среднем против O(n) у наивного перебора.

---

## 5. Цикл «использование → статистика → ребалансировка»

```mermaid
sequenceDiagram
    participant U as Пользователь / агент
    participant T as BKTree
    participant S as BKTreeStats
    participant C as BKTreeConfig

    U->>T: query("read_fiel", 2)
    T->>T: поиск с отсечением ветвей
    T->>S: record_read("read_file") ×N результатов<br/>(вес += глубина слова)
    T->>T: _queries_since_check += 1
    alt каждые rebalance_check_interval запросов
        T->>S: get_quality_score() = mean/sum
        S-->>T: quality_score
        alt quality_score < rebalance_quality_threshold
            T->>S: needs_rebalance = True
            T->>C: on_need_rebalance(tree, score) — коллбэк
            opt auto_rebalance == True
                T->>T: rebalance()
            end
        end
    end
    T-->>U: [("read_file", 2)]
```

---

## 6. Метрики расстояния — выбор

```mermaid
flowchart LR
    Q{Что сравниваем?}
    Q -- "короткие имена,<br/>опечатки (команды, инструменты)" --> LV[LevenshteinDistance<br/>int ≥ 0, метрика]
    Q -- "строки фикс. длины<br/>(хеши, коды)" --> HM[HammingDistance<br/>int, равная длина обязательна]
    Q -- "наборы символов/токенов,<br/>порядок не важен" --> JC[JaccardDistance<br/>float 0..1]
    Q -- "поиск по смыслу<br/>(фразы, документация)" --> SD[SemanticDistance<br/>float 0..2, косинусное<br/>+ кэш эмбеддингов]

    LV --> TREE((BKTree))
    HM --> TREE
    JC --> TREE
    SD --> TREE

    style TREE fill:#4a90d9,color:#fff
```

Наследование от `DistanceMetric`: единственный публичный метод `compute()`,
внутри вызывается `_round_distance()` (округление группирует детей узла;
например `precision` у Semantic/Jaccard регулирует число детей).

---

## 7. Статистика и метрика качества

```mermaid
flowchart TD
    subgraph "BKTreeStats"
        D1["depths: word → глубина<br/>ставится ОДИН РАЗ при add()<br/>меняется только при rebalance()"]
        D2["read_weights: Counter<br/>при каждом query(): вес[word] += depth[word]"]
        D3["quality = mean(read_weights) / sum(read_weights)"]
    end

    ADD(["add(word)"]) --> D1
    QUERY(["query() найдено слово"]) --> D2
    D2 --> CHECK{"_queries_since_check ≥<br/>rebalance_check_interval?"}
    CHECK -- да --> D3
    D3 --> THR{"quality < threshold?"}
    THR -- "да (нагрузка сконцентрирована<br/>в одном слове)" --> NR["needs_rebalance = True<br/>коллбэк / autorebalance"]
    THR -- нет --> OK["Продолжаем работу:<br/>распределение равномерное"]

    style NR fill:#d9534f,color:#fff
    style OK fill:#5cb85c,color:#fff
```

Идея: частые слова должны быть ближе к корню (меньше операций поиска),
чем ниже равномерность использования — тем больше выгода от пересборки.

---

## 8. Ребалансировка: было → стало

Пример из документации: `world` читается 90 раз (глубина 2), `helo` — 10 (глубина 1).

```mermaid
graph LR
    subgraph BEFORE ["До ребалансировки"]
        R1["hello<br/>depth 0"]
        H1["helo<br/>depth 1 · вес 10"]
        W1["world ★частое<br/>depth 2 · вес 180"]
        R1 -- "d=1" --> H1
        R1 -- "d=5" --> W1
    end

    subgraph AFTER ["После ребалансировки (полная пересборка по убыванию веса)"]
        R2["world ★частое<br/>depth 0, новый корень"]
        H2["helo<br/>depth 1"]
        W2["hello<br/>depth 1"]
        R2 -- "d=5" --> H2
        R2 -- "d=6" --> W2
    end

    BEFORE == "rebalance():<br/>1. sorted by weight<br/>2. reset() всего<br/>3. add() в новом порядке" ==> AFTER

    style W1 fill:#d9534f,color:#fff
    style R2 fill:#5cb85c,color:#fff
```

```mermaid
flowchart TD
    A(["rebalance()"]) --> B["sorted_words = stats.get_sorted_by_weight()<br/>(частые — первыми)"]
    B --> C["дописать слова без статистики в конец"]
    C --> D["root = None, size = 0,<br/>_all_words = [], stats.reset()"]
    D --> E["for word in sorted_words: add(word)"]
    E --> F(["Готово: частые слова у корня,<br/>глубины обновлены"])

    style D fill:#f0ad4e
    style F fill:#5cb85c,color:#fff
```

---

## 9. Три сценария использования

```mermaid
stateDiagram-v2
    [*] --> Построение: tree.append(tools)
    Построение --> Работа: query() / contains()
    Работа --> Проверка: каждые N запросов
    Проверка --> Работа: quality ≥ threshold
    Проверка --> Флаг: quality < threshold
    Флаг --> Работа: ручной путь<br/>if tree.needs_rebalance(): tree.rebalance()
    Флаг --> Ребаланс: auto_rebalance=True
    Флаг --> Коллбэк: on_need_rebalance(tree, q)<br/>(напр., очередь задач)
    Коллбэк --> Ребаланс: task_queue → rebalance()
    Ребаланс --> Работа
    Работа --> [*]: clear_stats() / сброс
```

| Сценарий | Конфиг | Кто вызывает `rebalance()` |
|----------|--------|---------------------------|
| A. Ручной | `auto_rebalance=False` | внешний код по `needs_rebalance()` |
| B. Автоматический | `auto_rebalance=True` | само дерево внутри `query()` |
| C. Event-driven | `on_need_rebalance=fn` | обработчик коллбэка (асинхронно) |

---

## 10. Интеграция в cascagent (ToolResolver)

```mermaid
flowchart LR
    IN["Запрос агента:<br/>tool = 'wriet_file'"] --> CONTAINS{"'wriet_file'<br/>in tree?<br/>(O(1) через depths)"}
    CONTAINS -- да --> ACCEPT["action: accept<br/>resolved_tool = точное имя"]
    CONTAINS -- нет --> QUERY["tree.query('wriet_file', max_distance=2)"]
    QUERY --> FOUND{"результаты<br/>не пусты?"}
    FOUND -- да --> FIX["resolved_tool = 'write_file'<br/>tool_typo_fixed = {original,<br/>resolved, distance}<br/>action: accept"]
    FOUND -- нет --> REJECT["tool_not_found<br/>action: reject<br/>metadata.error"]

    style ACCEPT fill:#5cb85c,color:#fff
    style FIX fill:#5cb85c,color:#fff
    style REJECT fill:#d9534f,color:#fff
```

Точки применения в cascagent: разрешение MCP-инструментов с опечатками,
поиск CLI-команд, семантический поиск по базе знаний, маршрутизация
запросов между агентами.

---

## 11. Полная картина: данные и поток управления

```mermaid
flowchart TB
    subgraph METRICS ["Метрики (DistanceMetric)"]
        LV[Levenshtein]
        SD[Semantic]
        HM[Hamming]
        JC[Jaccard]
    end

    subgraph CORE ["BKTree"]
        NODES["Дерево BKNode<br/>root + children{dist → node}"]
        LOCK["threading.Lock<br/>(потокобезопасный add)"]
        SEARCH["_search_recursive<br/>отсечение [d−m, d+m]"]
        RB["rebalance()<br/>полная пересборка"]
    end

    CFG["BKTreeConfig<br/>interval · threshold ·<br/>auto · callback"]
    STATS["BKTreeStats<br/>depths · read_weights ·<br/>quality · needs_rebalance"]

    WORDS(["Словарь:<br/>tools / commands / KB"]) -->|append/add| NODES
    REQ(["Запрос с опечаткой"]) -->|query| SEARCH
    SEARCH --> RES(["[(слово, расстояние)]<br/>по возрастанию d"])

    METRICS -->|compute| NODES
    METRICS -->|compute| SEARCH
    NODES <--> SEARCH
    STATS -->|depths: contains O1| CORE
    SEARCH -->|record_read| STATS
    STATS -->|quality| RB
    RB -->|пересборка по весу| NODES
    CFG -.->|параметры| CORE
    CFG -.->|callback| STATS

    style RES fill:#5cb85c,color:#fff
    style CORE fill:#4a90d9,color:#fff
```

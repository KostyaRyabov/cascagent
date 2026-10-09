# AGENTS.md — cascagent

Инструкция для AI-разработчиков, работающих над кодовой базой `cascagent`.
Этот файл — источник истины по архитектурным решениям. Прочитай его целиком
перед тем как писать код. Все решения здесь уже приняты — не перепроектируй.

---

## 1. Миссия

`cascagent` — мультиагентная система рекурсивной декомпозиции сложных задач
на атомарные подзадачи. Целевое железо: 4GB VRAM / 16GB RAM.

Принцип номер один: **LLM занимается только семантикой и рассуждениями**.
Всё остальное (маршрутизация, поиск инструментов, валидация, память,
формат, опечатки) — детерминированные CPU-алгоритмы.

Каждый агент работает в изолированной сессии с минимальным контекстом.

## 2. Архитектурные принципы (ОБЯЗАТЕЛЬНЫ)

### 2.1 Изоляция агентов
- Каждый вызов LLM — отдельный HTTP-запрос (не продолжение чата).
- Агент получает только свой task + минимум контекста родителя.
- Утечки контекста невозможны физически.

### 2.2 Минимизация контекста
- Не передавать LLM то, что можно вычислить на CPU.
- Не передавать MCP-схемы инструментов (2000+ токенов каждая).
- Selective Context для сжатия длинных выводов.

### 2.3 Перенос логики на CPU

| Задача | Решение | Модуль |
|--------|---------|--------|
| Выбор инструмента из 1000 | BK-tree (O(log n)) | `bk_tree.py` |
| Опечатки в именах | Левенштейн + фонетика | `bk_tree.fix_typo` |
| Похожие задачи | Эмбеддинги + cosine | `semantic.py` |
| Поиск в документации | RAG (BM25 + vectors) | `rag.py` |
| Валидация формата | YAML-парсер + эвристики | `yaml_heal.py` |
| Состояние задач | SQLite Kanban | `kanban.py` |
| Дубли подзадач | SequenceMatcher | `detector.py` |

### 2.4 Минимальный формат для LLM
Модель отвечает простым текстом, структура парсится на CPU:
```
read_file /tmp/x.txt        # хорошо
{"tool": "read_file", ...}  # плохо — модель думает о формате
```

## 3. Технологический стек

- Backend: **llama.cpp server** (`/v1/chat/completions`, save/restore KV
  через `/slots/N?action=save|restore`, prefix caching, flash attention,
  квантованный KV-кэш `--cache-type-k q8_0`).
- Модель: **Qwen3 4B Q4_K_M** (~3.0 GB), think/no_think через `"think"` параметр.
- Хранение: SQLite (Kanban), JSONL (история LLM-вызовов), `.bin` (KV-кэши).
- Python 3.11+: requests, sqlite3, difflib, numpy, Levenshtein, pyyaml, pytest.
- Лицензия Apache 2.0 (файлы LICENSE, NOTICE в корне).

Команда запуска сервера-референс:
```bash
./llama-server --model ~/models/qwen3-4b-q4_k_m.gguf \
  --host 127.0.0.1 --port 8080 --n-gpu-layers 999 --ctx-size 8192 \
  --flash-attn --cache-type-k q8_0 --cache-type-v q8_0 \
  --no-mmap --threads 6
```

## 4. Протокол декомпозиции

Категории строк ответа LLM:
- `>` RESEARCH — исследование, сбор информации
- `!` MUST_DO — обязательная задача каркаса
- `?` DEFERRED — опциональная, ждёт контекста, не детализируется
- `<atom>` — ровно одна строка, задача атомарна

System prompt модели зафиксирован в `parser.build_system_prompt()` —
менять текст можно только вместе с тестами.

## 5. Передача контекста между уровнями

- Родительский контекст: **только название родителя** (не описание).
- Siblings: чеклист выполненных братских задач (`✓ <title>`).
- Результаты `>`-задач добавляются в контекст детей.

## 6. Структура проекта

```
cascagent/
├── src/cascagent/
│   ├── models.py         # Task, TaskCategory, DecompositionCall  [готово]
│   ├── parser.py         # split/sanitize/detect/build_prompt     [готово]
│   ├── detector.py       # DuplicateDetector                       [готово]
│   ├── history.py        # History (JSONL + debug.log)             [Этап 1]
│   ├── client.py         # LlamaCppClient                          [Этап 2]
│   ├── cache_manager.py  # save/restore KV через /slots            [Этап 2]
│   ├── decomposer.py     # RecursiveDecomposer (DFS)               [Этап 3]
│   ├── cli.py            # argparse: --query --max-depth ...       [Этап 3]
│   ├── bk_tree.py        # BKTree + fix_typo                       [Этап 4]
│   ├── semantic.py       # SemanticMemory.remember/recall          [Этап 4]
│   └── rag.py            # BM25 + vector search                    [Этап 4]
├── tests/                # pytest, по файлу на модуль
├── docs/architecture.md  # конспект принятых решений
├── AGENTS.md             # этот файл
├── pyproject.toml
├── README.md
├── LICENSE / NOTICE
└── .gitignore
```

## 7. Этапы реализации

- **Этап 1 (Фундамент):** pyproject, models, parser, detector, history (+тесты)
- **Этап 2 (LLM-инфраструктура):** client, cache_manager
- **Этап 3 (Ядро):** decomposer, cli
- **Этап 4 (CPU-алгоритмы):** bk_tree, semantic, rag
- **Этап 5 (Интеграция):** оставшиеся тесты, README, docs

Текущий статус: см. раздел 10.

## 8. Принципы разработки

1. Никаких эвристик для определения атомарности: только точный `<atom>`
   или пустой final_response.
2. НЕ обрезать список подзадач — возвращать всё после дубль-фильтрации.
3. Честный парсинг: THINK и FINAL RESPONSE разделяются и логируются оба.
4. Без предварительного прогрева модели.
5. Каждый модуль тестируется независимо; чистые функции где возможно.
6. Коммит после каждого завершённого модуля.
7. Обновлять этот файл при реализации крупных компонентов.

## 9. Что НЕ нужно делать

- Мульти-модельные системы; draft model; warmup; ExLlamaV2; batch processing.
- Эвристики вида «если содержит hello world — это атом».

## 10. Статус и конвенции

Реализовано: `models.py`, `parser.py`, `detector.py` (+тесты).
Конвенции кода:
- `from __future__ import annotations` везде; type hints обязательны.
- Докейстры в каждом модуле со ссылкой на раздел этого файла.
- Тесты: `tests/test_<module>.py`, без сети и без llama.cpp (юнит-тесты
  только чистых функций; интеграционные помечать `@pytest.mark.network`).
- PEP 8, max line 88.

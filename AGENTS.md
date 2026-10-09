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
| Обогащение контекста | RAG + recall + сжатие | `enricher.py` |
| Состояние задач | SQLite Kanban | `kanban.py` |
| Дубли подзадач | SequenceMatcher | `detector.py` |
| Разбор формата ответа | отступы → brief/description | `parser.py` |
| Анализ провалов | ReflectAgent | `reflector.py` |

### 2.4 Минимальный формат для LLM
Модель отвечает простым текстом, структура парсится на CPU:
```
read_file /tmp/x.txt        # хорошо
{"tool": "read_file", ...}  # плохо — модель думает о формате
```
System prompt ~200 символов; модель не знает про RAG/память/Kanban (v2).

## 3. Технологический стек

- Backend: **llama.cpp server** (`/v1/chat/completions`, save/restore KV
  через `/slots/N?action=save|restore`, prefix caching, flash attention,
  квантованный KV-кэш `--cache-type-k q8_0`).
- Модель: **Qwen3 4B Q4_K_M** (~3.0 GB), think/no_think через `"think"` параметр.
- Хранение: SQLite (Kanban), JSONL (история LLM-вызовов), `.bin` (KV-кэши).
- Python 3.11+: requests, sqlite3, difflib, numpy, Levenshtein, pyyaml, pytest.
- Лицензия Apache 2.0 (файлы LICENSE, NOTICE в корне).
- Эволюация выбора движка (Ollama → llama.cpp → ExLlamaV2/TabbyAPI),
  сравнение моделей и конфигурации — `docs/stack.md`. Кандидат на ревизию
  (TabbyAPI + Qwen3 8B EXL2 4.0bpw) там же, §7 — решение не принято;
  до принятия код пишется под llama.cpp.

Команда запуска сервера-референс:
```bash
./llama-server --model ~/models/qwen3-4b-q4_k_m.gguf \
  --host 127.0.0.1 --port 8080 --n-gpu-layers 999 --ctx-size 8192 \
  --flash-attn --cache-type-k q8_0 --cache-type-v q8_0 \
  --no-mmap --threads 6
```

## 4. Протокол декомпозиции (v2 — ультра-простая архитектура)

> Пересмотр протокола: категории `> ! ?` УДАЛЕНЫ. Полная спецификация —
> `docs/protocol.md`. Системный промпт (~200 символов) зафиксирован в
> `parser.SYSTEM_PROMPT` — менять текст можно только вместе с тестами.

Формат вывода модели — простой текст с отступами:

```
Название задачи            <- без отступа = brief новой подзадачи
    Подробное описание     <- с отступом = description текущей
```

Атомарность: ровно `<atom>` (или пустой final_response) — никаких эвристик.
Исключение (тоже детерминированное, CPU): вырожденный ответ — одна
подзадача, чей brief ≈ задача родителя — трактуется как атом (§3 п.6).

Модель НЕ знает про категории, RAG, semantic memory, enrichment — всё это
делает CPU-слой вокруг неё (ContextEnricher, DuplicateDetector, Kanban).

Модель данных: `Task(id, brief, description, status, result, parent_id,
depth, subtasks)`; `TaskStatus = pending | running | done | failed`.
`TaskCategory` из v1 удалён.

## 5. Передача контекста между уровнями

USER-промпт вызова декомпозиции (см. docs/protocol.md §5):

```
Задача: {task.brief}

Контекст:
{enriched_context}          # RAG + semantic recall + siblings (собран CPU)

Выполненные ранее:
- {completed_siblings} ✓

РАЗБЕЙ НА ПОДЗАДАЧИ:
```

- Модель видит только свой task.brief + обогащённый контекст от enricher'а.
- think/no_think выбирается системой по глубине (L0–L2 think, L3+ no_think).
- Порядок подзадач в списке = порядок исполнения (зависимости решает система).

## 6. Структура проекта (v2)

```
cascagent/
├── src/cascagent/
│   ├── models.py         # Task(brief,description,status), TaskStatus  [миграция v2]
│   ├── parser.py         # SYSTEM_PROMPT, parse_decomposition, is_atomic [миграция v2]
│   ├── detector.py       # DuplicateDetector (по task.brief)            [готово*]
│   ├── history.py        # History (JSONL + debug.log)                   [Этап 1]
│   ├── client.py         # LlamaCppClient                                [Этап 2]
│   ├── cache_manager.py  # save/restore KV через /slots                  [Этап 2]
│   ├── decomposer.py     # DecomposerSession (stateful сессия)           [Этап 3]
│   ├── enricher.py       # ContextEnricher (RAG + semantic + siblings)   [Этап 4+]
│   ├── executor.py       # TaskExecutor                                  [Этап 4+]
│   ├── reflector.py      # ReflectAgent (анализ провалов)                [Этап 4+]
│   ├── researcher.py     # ResearchAgent (изолированный сбор инфы)       [Этап 4+]
│   ├── kanban.py         # SQLite хранилище                              [Этап 3]
│   ├── cli.py            # argparse: --query --max-depth ...             [Этап 3]
│   ├── bk_tree.py        # BKTree + fix_typo                             [Этап 4]
│   ├── semantic.py       # SemanticMemory.remember/recall                [Этап 4]
│   └── rag.py            # BM25 + vector search                          [Этап 4]
├── tests/                # pytest, по файлу на модуль
├── docs/protocol.md      # СПЕЦИФИКАЦИЯ ПРОТОКОЛА v2 (источник истины)
├── docs/architecture.md  # конспект принятых решений
├── docs/product.md       # продукт: сценарии, функции, системные агенты (draft)
├── docs/cpu-offload.md   # реестр CPU-алгоритмов vs LLM-вызовы
├── docs/stack.md         # стек инференса: движки, модели, think, конфиги (draft)
├── docs/data.md          # модель данных: Task v2, dot-path ID, JSONL, схема SQLite
├── docs/prompts.md       # промпты всех агентов, парсинг вывода, антипаттерны
├── docs/config.md        # зависимости core/extras, config.toml, структура
├── docs/algorithms.md    # эталонные реализации CPU-алгоритмов (BK-tree, fuzzy, memory, RAG)
├── docs/performance.md   # кэш L1–L3, префилл, draft model, PerformanceMetrics
├── docs/process.md       # процесс: ленивая декомпозиция, enrichment, Reflect/Research (Глава IX)
├── docs/examples.md      # референсные сквозные логи + anti-примеры (Глава XII)
├── docs/operations.md    # эксплуатация: железо, установка, мониторинг, troubleshooting (Глава XI)
├── docs/plugins.md       # плагинная архитектура агентов v3: секции, SessionMemory, конфиги (Этап 5+)
├── docs/roadmap.md       # АУДИТ ПРОБЕЛОВ + решения: Python для MVP (порт после), timeout/guard/recovery, MVP-чеклист
├── docs/decisions/       # ADR-001..004 (движок, минимальный промпт, без категорий, ленивая декомпозиция)
├── AGENTS.md             # этот файл
├── pyproject.toml
├── README.md
├── LICENSE / NOTICE
└── .gitignore
```

\* логика готова, API адаптируется под `task.brief` при миграции v2.

Модели максимально простые, вся сложность — в CPU-слое вокруг LLM.

## 7. Этапы реализации (v2)

- **Этап 1 (Фундамент):** миграция models/parser/detector на протокол v2
  (+тесты), history.py (+тесты; формат записи — docs/data.md §3). pyproject — готов.
- **Этап 2 (LLM-инфраструктура):** client, cache_manager
- **Этап 3 (Ядро):** decomposer (DecomposerSession + execute_with_decomposition),
  kanban, cli
- **Этап 4 (CPU-алгоритмы):** bk_tree, semantic, rag
- **Этап 5 (Обвязка агентов):** enricher, executor, reflector, researcher
- **Этап 6 (Интеграция):** оставшиеся тесты, README, docs

Процесс выполнения системы (ленивая декомпозиция, enrichment pipeline,
Research/Reflect, суммаризация) специфицирован в `docs/process.md`;
референсные сквозные логи — `docs/examples.md`; эксплуатация —
`docs/operations.md`. Плагинная архитектура агентов (секции init/input/
trigger/output, SessionMemory, конфигурации через TOML) спроектирована в
`docs/plugins.md` и реализуется на Этапе 5. Аудит непроектированных областей
(20 пробелов) и принятые по ним решения — `docs/roadmap.md`; перед реализацией
новых компонентов сверяйтесь с его чек-листом продакшн-MVP (§8) и открытыми
вопросами (§9).

Текущий статус: см. раздел 10.

## 8. Принципы разработки

0. **Язык: MVP пишется на Python; после MVP — оптимизация/порт на более
   подходящий язык** (решения и следствия — `docs/roadmap.md` §1). Не
   завязывать CPU-алгоритмы на Python-specific трюки, держать форматы на
   диске language-neutral.
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
- Просить LLM делать то, что есть в реестре CPU-алгоритмов (`docs/cpu-offload.md` §2):
  парсить формат, искать дубли, выбирать инструменты, планировать очередь,
  хранить/читать состояние. На GPU остаются ровно 5 видов вызовов (§3 того же файла).

## 10. Статус и конвенции

**Архитектура: v2 (ультра-простая)** — принята, документация зафиксирована:
`docs/protocol.md` (источник истины по формату), AGENTS.md §4–6, architecture.md.

Реализовано (код ещё на протоколе v1, миграция — следующий шаг):
`models.py`, `parser.py`, `detector.py` (+тесты).

Ближайшие шаги (порядок):
1. Миграция `models.py` + `parser.py` + `detector.py` на v2 (+переписать тесты);
2. `history.py` (JSONL + debug.log) + тесты;
3. Этап 2: `client.py`, `cache_manager.py`;
4. Далее по AGENTS.md §7 с новыми модулями v2 (enricher/executor/reflector/researcher).

Конвенции кода:
- `from __future__ import annotations` везде; type hints обязательны.
- Докейстры в каждом модуле со ссылкой на раздел этого файла / docs/protocol.md.
- Тесты: `tests/test_<module>.py`, без сети и без llama.cpp (юнит-тесты
  только чистых функций; интеграционные помечать `@pytest.mark.network`).
- PEP 8, max line 88.

# cascagent — Архитектура

> Обновлено под **архитектуру v2** (ультра-простая декомпозиция).
> Полная спецификация протокола — `docs/protocol.md`.
> Продуктовое видение и системные агенты — `docs/product.md`.
> Реестр CPU-алгоритмов (что делает CPU, чтобы не нагружать GPU) — `docs/cpu-offload.md`.
> Стек инференса (движки, модели, think-режим, конфигурация) — `docs/stack.md`.
> Статусы модулей: ✅ реализовано, 🚧 в работе/миграции, ⬜ план.

## 1. Обзор системы

cascagent рекурсивно декомпозирует задачу пользователя на дерево атомарных
подзадач. LLM (Qwen3 4B на llama.cpp) вызывается изолированно для каждой
декомпозиции с **минимальным промптом (~480 символов)**; модель не знает про
категории, RAG, память и формат — вся обвязка детерминированные CPU-алгоритмы.

```
                ┌────────────── ОРКЕСТРАТОР (CPU) ──────────────┐
CLI (--query)──► DecomposerSession ──► LlamaCppClient ──► llama.cpp server
      │               ▲   │                                      │
      │        Enricher│   ▼ raw text                            ▼
      │      (RAG+seman)│  parser.parse_decomposition ──► Task[] │
      │               │   ▼                                     │
      │        DuplicateDetector → Kanban (SQLite) ◄── Executor ┘
      │               ▼
      └── tree/statistics   History (JSONL + debug.log)
                              ReflectAgent / ResearchAgent (при сбоях)
```

Формат вывода модели: `Название` (без отступа) + `    Описание` (с отступом);
атом — ровно `<atom>`; вырожденный повтор родителя = атом (§3.7).
Details: docs/protocol.md §2–3.

## 2. Модули и статусы

| Модуль | Назначение | Статус |
|--------|-----------|--------|
| `models.py` | Task(brief, description, status), TaskStatus, DecompositionCall | 🚧 миграция v1→v2 |
| `parser.py` | SYSTEM_PROMPT, split_think_and_response, sanitize_line, parse_decomposition, is_atomic, build_user_prompt | 🚧 миграция v1→v2 |
| `detector.py` | DuplicateDetector (SequenceMatcher, порог 0.75, по brief) | 🚧 адаптация API |
| `history.py` | JSONL история LLM-вызовов + debug.log (THINK / FINAL RESPONSE) | ⬜ Этап 1 |
| `client.py` | OpenAI-совместимый клиент llama.cpp, think параметр | ⬜ Этап 2 |
| `cache_manager.py` | save/restore KV-кэша через /slots API | ⬜ Этап 2 |
| `decomposer.py` | DecomposerSession: одна задача = один изолированный вызов | ⬜ Этап 3 |
| `kanban.py` | SQLite хранилище состояний задач | ⬜ Этап 3 |
| `cli.py` | argparse интерфейс, рендер дерева и статистики | ⬜ Этап 3 |
| `enricher.py` | ContextEnricher: RAG + semantic recall + siblings + сжатие | ⬜ Этап 4+ |
| `executor.py` | TaskExecutor (выполнение атомарной задачи) | ⬜ Этап 4+ |
| `reflector.py` | ReflectAgent: анализ провалов, обратная связь | ⬜ Этап 4+ |
| `researcher.py` | ResearchAgent: изолированный сбор информации | ⬜ Этап 4+ |
| `bk_tree.py` | BK-tree fuzzy поиск инструментов, fix_typo | ⬜ Этап 4 |
| `semantic.py` | SemanticMemory (эмбеддинги + cosine, remember/recall) | ⬜ Этап 4 |
| `rag.py` | BM25 + vector search по документации | ⬜ Этап 4 |

Полный реестр CPU-алгоритмов (что делает CPU, чтобы не нагружать GPU/LLM) —
`docs/cpu-offload.md`; продуктовые сценарии и таблица системных агентов —
`docs/product.md`.

## 3. Протокол декомпозиции

Спецификация — `docs/protocol.md` (источник истины, инварианты парсера и
degenerate-правило §3 п.6 формулируются там же и не пересказываются здесь).
Для архитектуры важны два следствия: разбор вывода — ~20 строк детерминиро-
ванного CPU-кода, и ни одно решение об атомарности/обрезке не принимается
эвристиками (principles.md §3).

## 4. Контекст агента (что уходит в prompt)

Состав USER-промпта зафиксирован в docs/protocol.md §5; сборка — эталонный
ContextEnricher в process.md §2 (обогащение строится на CPU по
embedding(brief), LLM не решает что искать); механика think/no_think —
stack.md §5. Архитектурное следствие для всех модулей: **никакой блок
контекста не появляется в промпте без CPU-конструктора** — модель видит
только готовый текст.

## 5. Хранилища

Схема SQLite, формат JSONL-записей и debug.log — **`docs/data.md` §3–4**
(источник истины). Здесь — только расстановка ролей:
- **SQLite Kanban** — единственный источник истины о состоянии задач и графе;
- **JSONL + SQL-зеркало** — сырьё для метрик и будущего файн-тюна;
- **debug.log** — человекочитаемая отладка (grep по task_id);
- **KV-кэш `.bin`** — персистентность префиксов llama.cpp (performance.md §1, L2).

## 6. Решения, которые менять нельзя

Обоснованы в AGENTS.md §3, §8, §9: llama.cpp (а не ExLlamaV2), одна модель,
без warmup, без batch, формат ответа — plain text с отступами (v2),
короткий системный промпт без упоминания внутренних механизмов.
Эволюция и обоснование выбора стека — `docs/stack.md` §1–2; там же
задокументирован **кандидат на ревизию** TabbyAPI + Qwen3 8B EXL2 (§7).

## 7. Смежные документы (карта источников истины)

Каждая тема имеет один «домашний» документ; остальные ссылаются, не
пересказывая:

| Документ | Тема (источник истины по умолчанию) |
|---|---|
| `docs/principles.md` | философия и архитектурные принципы, название, лицензия |
| `docs/protocol.md` | протокол общения с моделью v2 (формат вывода, Task-контракт) |
| `docs/product.md` | продукт: миссия, сценарии, системные агенты, state machine |
| `docs/cpu-offload.md` | реестр CPU-алгоритмов P1–P9 / A1–A13 vs LLM-вызовы |
| `docs/algorithms.md` | эталонные реализации CPU-алгоритмов (BK-tree, fuzzy, memory, RAG, YAML) |
| `docs/performance.md` | кэш L1–L3, префилл, draft model, PerformanceMetrics |
| `docs/stack.md` | стек инференса: движки, модель, think, KV-кэш, бюджет токенов |
| `docs/data.md` | модель данных: Task, ID, история вызовов, схема SQLite, EnrichedContext |
| `docs/prompts.md` | промпты всех агентов, парсинг вывода, антипаттерны |
| `docs/config.md` | зависимости core/extras, config.toml, структура проекта |
| `docs/process.md` | процесс: ленивая декомпозиция, enrichment, Reflect/Research (Глава IX) |
| `docs/examples.md` | референсные сквозные логи + anti-примеры (Глава XII); основа e2e |
| `docs/operations.md` | эксплуатация: железо, установка, мониторинг, troubleshooting (Глава XI) |
| `docs/plugins.md` | плагинная архитектура агентов v3: секции, SessionMemory, TOML-конфиги (Этап 5+) |
| `docs/roadmap.md` | аудит пробелов (20) + решения: timeout/guard/recovery, чек-лист продакшн-MVP |
| `docs/decisions/` | ADR 001–004 (движок, минимальный промпт, без категорий, ленивая декомпозиция) |

Решения «CPU-offload», «изоляция агентов» (principles.md §2.1) и
«атомарность только `<atom>`» (principles.md §3, AGENTS.md §8.1) приняты и
задокументированы в тематических файлах; отдельные ADR для них не заводились.

## 8. План заполнения документа

- После миграции v2: зафиксировать финальные сигнатуры models/parser.
- После Этапа 2: разделы «Клиент и think-режим», «KV-кэш префиксов».
- После Этапа 3: «Алгоритм DecomposerSession», «Формат вывода CLI» (end-to-end).
- После Этапа 4: «BK-tree», «SemanticMemory», «RAG», «ContextEnricher».

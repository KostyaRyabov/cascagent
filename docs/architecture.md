# cascagent — Архитектура

> Обновлено под **архитектуру v2** (ультра-простая декомпозиция).
> Полная спецификация протокола — `docs/protocol.md`.
> Статусы модулей: ✅ реализовано, 🚧 в работе/миграции, ⬜ план.

## 1. Обзор системы

cascagent рекурсивно декомпозирует задачу пользователя на дерево атомарных
подзадач. LLM (Qwen3 4B на llama.cpp) вызывается изолированно для каждой
декомпозиции с **минимальным промптом (~200 символов)**; модель не знает про
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
атом — ровно `<atom>`; дедупликация по правилам R1/R2 (protocol.md §3.8):
локальные дубли удаляются (пусто после фильтрации ⇒ атом), кросс-ветковые
совпадения становятся ссылкой `duplicate_of` на самый глубокий оригинал.
Details: docs/protocol.md §2–3, §3.8.

## 2. Модули и статусы

| Модуль | Назначение | Статус |
|--------|-----------|--------|
| `models.py` | Task(brief, description, status, duplicate_of), TaskStatus, DecompositionCall | 🚧 миграция v1→v2 |
| `parser.py` | SYSTEM_PROMPT, split_think_and_response, sanitize_line, parse_decomposition, is_atomic, build_user_prompt | 🚧 миграция v1→v2 |
| `detector.py` | DuplicateDetector: R1 локальный (порог 0.75), R2 глобальный (порог ~0.90, по brief) | 🚧 адаптация API |
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

## 3. Протокол декомпозиции

См. `docs/protocol.md` (спецификация v2). Ключевые инварианты парсера:
- Атомарность определяется **только** точной строкой `<atom>` или пустым
  final_response (никаких эвристик).
- Список подзадач не обрезается; отбраковываются только дубликаты,
  повтор родителя и циклы по предкам (DuplicateDetector).
- Строка без отступа = brief; отступ = продолжение description; переносы
  описания склеиваются в одну строку.
- Вырожденный ответ (одна подзадача ≈ родитель) трактуется как атом —
  детерминированное CPU-правило, docs/protocol.md §3 п.6.

## 4. Контекст агента (что уходит в prompt)

Минимальная сборка (docs/protocol.md §5):
1. system prompt (`SYSTEM_PROMPT`, фиксированный ~200 симв. → префиксный кэш);
2. `Задача: {task.brief}`;
3. `Контекст:` — enriched context (RAG-фрагменты, semantic recall top-3,
   сжатые выводы) — источники модели неизвестны;
4. `Выполненные ранее:` — чеклист siblings;
5. `РАЗБЕЙ НА ПОДЗАДАЧИ:`.

think/no_think выбирается системой по глубине (L0–L2 think, L3+ no_think).

## 5. Хранилища

- **SQLite Kanban** — источник истины о состоянии задач
  (pending/running/done/failed), граф parent_id + порядок исполнения.
  Схема фиксируется в `kanban.py` (Этап 3). Хранит и связи дедупликации:
  колонка `duplicate_of` → ссылка на оригинал (правило R2, protocol.md §3.8);
  поиск глобальных дублей — по нормализованному `brief` через BKTree.
- **JSONL** — один вызов LLM = одна строка (DecompositionCall.to_dict()).
- **debug.log** — человекочитаемый лог с раздельными секциями
  `=== THINK ===` / `=== FINAL RESPONSE ===`.
- **KV-кэш `.bin`** — сохранённые слоты llama.cpp для общих префиксов
  (стабильный SYSTEM_PROMPT — идеальный кандидат).

## 6. Решения, которые менять нельзя

Обоснованы в AGENTS.md §3, §8, §9: llama.cpp (а не ExLlamaV2), одна модель,
без warmup, без batch, формат ответа — plain text с отступами (v2),
короткий системный промпт без упоминания внутренних механизмов.

## 7. План заполнения документа

- После миграции v2: зафиксировать финальные сигнатуры models/parser.
- После Этапа 2: разделы «Клиент и think-режим», «KV-кэш префиксов».
- После Этапа 3: «Алгоритм DecomposerSession», «Формат вывода CLI» (end-to-end).
- После Этапа 4: «BK-tree», «SemanticMemory», «RAG», «ContextEnricher».

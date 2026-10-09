# cascagent — Архитектура

> Каркас документа. Разделы заполняются по мере реализации этапов
> (см. AGENTS.md §7). Статусы: ✅ реализовано, 🚧 в работе, ⬜ план.

## 1. Обзор системы

cascagent рекурсивно декомпозирует задачу пользователя на дерево атомарных
подзадач. LLM (Qwen3 4B на llama.cpp) вызывается изолированно для каждой
декомпозиции; вся обвязка — детерминированные CPU-алгоритмы.

```
CLI (--query) ──► RecursiveDecomposer ──► LlamaCppClient ──► llama.cpp server
      ▲                  │    ▲                   │
      │                  │    │                   ▼
  DuplicateDetector   Kanban │              parser.py (CPU)
  (фильтр дублей)     (SQLite)│              models.Task[]
                              └── History (JSONL + debug.log)
```

## 2. Модули и статусы

| Модуль | Назначение | Статус |
|--------|-----------|--------|
| `models.py` | Task, TaskCategory, DecompositionCall | ✅ |
| `parser.py` | split_think_and_response, sanitize_line, detect_category, build_system_prompt | ✅ |
| `detector.py` | DuplicateDetector (SequenceMatcher, порог 0.75) | ✅ |
| `history.py` | JSONL история LLM-вызовов + debug.log | 🚧 Этап 1 |
| `client.py` | OpenAI-совместимый клиент llama.cpp, think параметр | ⬜ Этап 2 |
| `cache_manager.py` | save/restore KV-кэша через /slots API | ⬜ Этап 2 |
| `decomposer.py` | DFS-рекурсия, max_depth, think-depth | ⬜ Этап 3 |
| `cli.py` | argparse интерфейс, рендер дерева и статистики | ⬜ Этап 3 |
| `bk_tree.py` | BK-tree fuzzy поиск инструментов, fix_typo | ⬜ Этап 4 |
| `semantic.py` | SemanticMemory (эмбеддинги + cosine) | ⬜ Этап 4 |
| `rag.py` | BM25 + vector search по документации | ⬜ Этап 4 |

## 3. Протокол декомпозиции

См. AGENTS.md §4. Ключевые инварианты парсера:
- Атомарность определяется **только** точной строкой `<atom>` или пустым
  final_response (никаких эвристик).
- Список подзадач не обрезается; отбраковываются только дубликаты,
  повтор родителя и циклы по предкам.

## 4. Контекст агента (что уходит в prompt)

Минимальная сборка (AGENTS.md §5):
1. system prompt (фиксированный, кэшируемый префикс → prefix caching);
2. `РОДИТЕЛЬ [<sym>] <title>` — только название;
3. `ЧТО УЖЕ ВЫПОЛНЕНО` — чеклист siblings (`✓ ...`);
4. результаты RESEARCH-задач (если были);
5. `ЗАДАЧА [<sym>] <title>`.

## 5. Хранилища

- **SQLite Kanban** — источник истины о состоянии задач (todo/doing/done),
  граф зависимостей parent_id. Схема фиксируется в `kanban.py` (Этап 2+).
- **JSONL** — один вызов LLM = одна строка (DecompositionCall.to_json()).
- **debug.log** — человекочитаемый лог с раздельными секциями
  `=== THINK ===` / `=== FINAL RESPONSE ===`.
- **KV-кэш `.bin`** — сохранённые слоты llama.cpp для общих префиксов.

## 6. Решения, которые менять нельзя

Обоснованы в AGENTS.md §3, §8, §9: llama.cpp (а не ExLlamaV2), одна модель,
без warmup, без batch, формат ответа — plain text.

## 7. План заполнения документа

- После Этапа 2: разделы «Клиент и think-режим», «KV-кэш префиксов».
- После Этапа 3: «Алгоритм decomposer», «Формат вывода CLI» (пример end-to-end).
- После Этапа 4: «BK-tree», «SemanticMemory», «RAG».

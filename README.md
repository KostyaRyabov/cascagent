# cascagent

Ультра-простой каскадный агент: вся сложность — на CPU вокруг LLM, модель
видит только короткий текстовый протокол (спецификация — `docs/protocol.md`).

## Статус: v0.1 (прототип ядра, не CLI-продукт)

Реализовано (`pytest`, без сети):
- `models.py` — Task v2 (brief/description/status, snowflake-ID, embed);
- `memory.py` — Memory (flat-KV + glob-подписки), иерархия Workspace → Project → Agent;
- `stream_parser.py` — ResponseParser: стрим-парсер вывода LLM (Segment/Word v5);
- `plugins/` — Pre/Runtime/Post-секции; встроенный TaskParserPlugin
  (SYSTEM_PROMPT декомпозиции, парсинг отступов, дедуп 0.75, атомарность);
- `bk_tree.py` — fuzzy-поиск инструментов (Левенштейн + фонетика);
- `client.py` — OpenAI-совместимый клиент llama.cpp (think-параметр, chunk-iterator);
- `kanban.py` — SQLite-хранилище состояний задач (статусы, очереди).

Не реализовано (план — `docs/roadmap.md`, Фаза 2): агенты и CPU-диспетчер
(`executor.py`, `orchestrator.py`, `researcher.py`, `decomposer.py`,
`summarizer.py`, `validator.py`, `reflector.py`), оркестратор-цикл
`dispatch_loop` (PENDING → ENRICHMENT → RUNNING → DONE/FAILED, см. ниже),
CLI, память/RAG, тесты клиента и канбан-доски. Полная карта модулей и
статусов — `docs/architecture.md` §2; этапы — AGENTS.md §7.

## Архитектура в двух словах (ролевая модель v7)

Агент — это не «промпт + инструменты», а полный конвейер обработки одной
задачи. Детерминированный **CPU-диспетчер живёт внутри каждого агента** и
ведёт задачу по этапам; LLM отвечает только за содержательную часть:

```
PENDING ──► ENRICHMENT ──────────────► RUNNING ──► оценка ──► DONE / FAILED
 (очередь)   PrePlugin-секция:          тело агента        PostPlugin /
             декомпозиция + выбор       (Executor или       Validator
             роли по фактам:            Orchestrator)       (флаги →
             атом → Executor,                              диспетчеру)
             неатом → Orchestrator
```

Роль привязывается ПОСЛЕ PrePlugin-секции: «сбой» Decomposer'а (задача
оказалась атомарной) — штатный путь, а не ошибка.

Шесть ролей агентов (подробно — `docs/product.md` §4–5):
**Decomposer** (один уровень детализации), **Executor** (атомарные задачи,
MCP tools, может звать Researcher как инструмент), **Researcher** (единственный,
кто ищет информацию — изоляция контекста остальных), **Orchestrator**
(неатомные задачи как оркестратор своего уровня: ждёт детей через
`kanban.park()/maybe_wake_parent()`, подтверждает DONE, редекомпозирует или
задаёт вопрос пользователю; он же аварийный режим для тупиков),
**Summarizer** (опциональный, триггер по бюджету у CPU), **Validator**
(PostPlugin-проверка результата).

Правило разделения: **CPU решает «когда» и «кому», LLM решает «что»**.
Агенты не вызывают друг друга напрямую — плагины пишут флаги, их читает
диспетчер после `run()`.

## Документация
Начинать с `AGENTS.md`; философия — `docs/principles.md`; протокол —
`docs/protocol.md`; жизненный цикл и dispatch_loop — `docs/process.md` §1.2;
плагинная архитектура — `docs/plugins.md`; решения —
`docs/decisions/` (ADR 001–005).

License: Apache-2.0 (см. LICENSE / NOTICE).

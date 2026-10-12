# cascagent

Ультра-простой каскадный агент: вся сложность — на CPU вокруг LLM, модель
видит только короткий текстовый протокол (спецификация — `docs/protocol.md`).

## Статус: v0.1 (прототип ядра, не CLI-продукт)

Реализовано и покрыто тестами (`pytest`, без сети):
- `models.py` — Task v2 (brief/description/status, snowflake-ID, embed);
- `memory.py` — Memory (flat-KV + glob-подписки), иерархия Workspace → Project → Agent;
- `stream_parser.py` — ResponseParser: стрим-парсер вывода LLM (Segment/Word v5);
- `plugins/` — Pre/Runtime/Post-секции; встроенный TaskParserPlugin
  (SYSTEM_PROMPT декомпозиции, парсинг отступов, дедуп 0.75, атомарность);
- `bk_tree.py` — fuzzy-поиск инструментов (Левенштейн + фонетика).

Не реализовано (план — `docs/roadmap.md`, Фаза 2): клиент llama.cpp,
history, kanban, оркестратор, CLI. Полная карта модулей и статусов —
`docs/architecture.md` §2; этапы — AGENTS.md §7.

## Документация
Начинать с `AGENTS.md`; философия — `docs/principles.md`; протокол —
`docs/protocol.md`; плагинная архитектура — `docs/plugins.md`; решения —
`docs/decisions/` (ADR 001–004).

License: Apache-2.0 (см. LICENSE / NOTICE).

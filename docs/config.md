# cascagent — Конфигурация, зависимости, структура проекта

> Статус: **проект v2**. Документ детализирует Главу IV (стек хранения и
> Python-окружение) полной документации. Фактические дефолты кода — в
> модулях; этот файл — источник истины по формату `config.toml`.

## 1. Python окружение

- **Python 3.11+** (requires-python `>=3.11`): быстрее 3.10 (до ~25%),
  `Self`/TypeVar в typing, лучший error reporting, широкая поддержка библиотек.
- venv + editable install: `python -m venv .venv && pip install -e ".[dev]"`.
- Инструменты: **ruff** (линт+формат, замена black/isort/flake8), **pytest**
  (тесты; интеграционные — маркер `@pytest.mark.network`), **mypy** (типы).
  pre-commit — при появлении CI (вне скоупа Этапа 1).

## 2. Зависимости (pyproject.toml)

Принцип: ядро — минимальное, тяжёлое — extras (не тормозит установку ядра и
не тянет GPU-зависимости).

| Пакет | Где | Зачем | Статус |
|-------|-----|-------|--------|
| requests | core | HTTP к llama.cpp `/v1/chat/completions` | ✅ в pyproject |
| numpy | core | cosine similarity эмбеддингов (semantic recall P2) | ✅ в pyproject |
| PyYAML | core | разбор альтернативного формата вывода (толерантный парсер A2) | ✅ в pyproject |
| python-Levenshtein | core | fix_typo/BK-tree (P4) | ✅ в pyproject |
| pytest | dev | тесты | ✅ в pyproject |
| click | cli | CLI (`cascagent = cascagent.cli:main`) | ⚠️ в scripts, но НЕ в deps — см. §2.1 |
| rich | cli | прогресс-бар дерева задач | ⏳ extras `cli` |
| sentence-transformers | semantic | эмбеддинги (альтернатива — API бэкенда) | ⏳ extras `semantic` |
| rank-bm25 | rag | BM25 поверх SQLite FTS5 | ⏳ extras `rag` |
| faiss-cpu | rag | vector search при базе >10k | ⏳ extras `rag` |
| httpx, beautifulsoup4 | researcher | async HTTP + HTML для ResearchAgent | ⏳ extras `research` |
| selective-context | enricher | сжатие длинных выводов (P5) | ⏳ extras `enrich`; fallback — own gzip+freq impl |

### 2.1 Расхождения с Главой IV (зафиксировано осознанно)

1. В оригинале `requests` подписан как «HTTP клиент для TabbyAPI» — у нас
   бэкенд-агностичный OpenAI-compatible интерфейс (ADR-001); комментарий
   должен гласить «llama.cpp / любой OpenAI API».
2. `click` уже объявлен в `[project.scripts]`, но отсутствует в зависимостях —
   на Этапе 3 либо добавить в core, либо остаться на stdlib argparse
   (решение фиксируется вместе с `cli.py`; AGENTS.md §6 исторически писал
   «argparse»). До этого момента — не баг runtime, т.к. cli ещё нет.
3. Тяжёлые пакеты (sentence-transformers, faiss, selective-context) НЕ идут в
   core-deps, в отличие от Главы IV: ядро Этапов 1–3 обязано ставиться и
   тестироваться без них (чистые юнит-тесты не требуют сети/GPU-библиотек).

## 3. Структура проекта

Действующая (AGENTS.md §6) дополняется документами этой папки:

```
cascagent/
├── src/cascagent/          # см. AGENTS.md §6 (models/parser/detector/history/
│   └── ...                 #  client/cache_manager/decomposer/kanban/cli/
│                           #  bk_tree/semantic/rag/enricher/executor/
│                           #  reflector/researcher)
├── tests/                  # test_<module>.py; integration — end-to-end на моках
├── docs/
│   ├── protocol.md         # СПЕЦИФИКАЦИЯ протокола v2 (источник истины формата)
│   ├── principles.md       # философия и архитектурные принципы, название, лицензия
│   ├── product.md          # продукт: миссия, сценарии, системные агенты
│   ├── architecture.md     # конспект архитектурных решений
│   ├── cpu-offload.md      # реестр CPU-алгоритмов (P/A/E) vs LLM-вызовы
│   ├── algorithms.md       # эталонные реализации: BK-tree, fuzzy, memory, RAG, YAML
│   ├── performance.md      # кэш L1-L3, префилл, draft model, PerformanceMetrics
│   ├── stack.md            # стек инференса: движки, модель, think, KV-кэш
│   ├── data.md             # модель данных: Task, ID, история, хранилища
│   ├── prompts.md          # системные промпты всех агентов, парсинг, антипаттерны
│   ├── config.md           # этот файл: конфиг, зависимости, окружение
│   ├── process.md          # процесс: ленивая декомпозиция, enrichment,
│   │                       #   Reflect/Research (Глава IX)
│   ├── examples.md         # референсные сквозные логи + anti-примеры (Глава XII)
│   ├── operations.md       # эксплуатация: железо, установка, мониторинг (Глава XI)
│   ├── plugins.md          # плагинная архитектура агентов v3: секции, память, конфиги
│   ├── roadmap.md          # аудит пробелов + решения (MVP-чеклист, timeout/guard/recovery)
│   └── decisions/          # ADR: 001 inference engine, 002 minimal prompt,
│       │                   #      003 no categories
│       └── *.md
├── examples/               # hello_world.py, rest_api.py — Этап 6
├── AGENTS.md  README.md  LICENSE  NOTICE  pyproject.toml  .gitignore
```

Отличия от Главы IV.4 (чтобы не вводить в заблуждение):
- `.pre-commit-config.yaml`/`.ruff.toml` — добавляются при настройке CI
  (сейчас линт-конвенции зафиксированы текстом: PEP8, max line 88);
- `docs/api.md`/`examples.md` из оригинала переименованы: api → protocol.md +
  докейстры модулей, примеры → examples/ + product.md §2.5 (сценарии).

## 4. config.toml

Файл опционален — все значения имеют дефолты в коде. Секции:

```toml
[llm]
url = "http://127.0.0.1:8080/v1/chat/completions"   # llama.cpp (TabbyAPI :5000 — при ADR-001 Proposed)
model = "local"
temperature = 0.2
max_tokens = 4000
timeout = 300                     # секунды; таймаут -> retry без LLM (cpu-offload E1)

[decomposer]
max_depth = 20                    # жёсткий потолок рекурсии (CPU-guard)
similarity_threshold = 0.75       # порог SequenceMatcher (детектор дублей/циклов)
think_max_depth = 2               # L0..L2 think, глубже no_think (A7)

[storage]
db_path = "./data/kanban.db"
history_path = "./data/history.jsonl"
debug_log_path = "./data/debug.log"
backup_every_n_ops = 200          # sqlite backup (data.md §4.4)

[enrichment]
max_context_tokens = 500          # бюджет блока «Контекст»
rag_top_k = 3
semantic_top_k = 3
enable_selective_context = true

[research]
max_iterations = 5
web_search_enabled = false        # интернет — только явно включённый флаг
docs_search_enabled = true
```

Отличие от Главы IV.5: убран флаг `think_enabled` глобально — think/no_think
решает система по глубине (prompts.md §5), а не конфиг; `llm.url` по умолчанию
указывает на действующий бэкенд (llama.cpp :8080), не кандидатский TabbyAPI.

## 5. Приоритет конфигурации

1. CLI-аргументы (высший)
2. Переменные окружения `CASCAGENT_<SECTION>_<KEY>` (напр. `CASCAGENT_LLM_URL`)
3. `config.toml` (`--config` или `./config.toml`)
4. Дефолты в коде

Загрузка — один чистый модуль `config.py` (Этап 3, вместе с cli.py):
dataclass per section, валидация типов на CPU, неизвестные ключи — warning
(не crash).

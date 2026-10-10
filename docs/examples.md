# cascagent — Примеры работы системы (сквозные сценарии)

> Статус: **проект v2**. Детализация Главы XII полной документации.
> Логи ниже — **референсные**: они показывают целевое поведение CLI и
> оркестратора до того, как `cli.py` написан (Этап 3); после реализации
> эти сценарии становятся основой для e2e-тестов (`tests/e2e/`).
> Формат вывода модели — protocol §3; статусы — product §5; метрики —
> performance §5. Категории `> ! ? ⚛` из оригинала Глав удалены (ADR-003) —
> в логах их нет намеренно. Процессные детали — `docs/process.md`.

## 1. Hello World (минимальный путь: атом без декомпозиции)

```bash
cascagent --query "Написать программу Hello World на Python"
```

```
Задача: Написать программу Hello World на Python
Бэкенд: llama.cpp :8080 | Qwen3 4B Q4_K_M | think: L0=ON | max_depth=20

[0] Декомпозиция корня...
    ↳ FINAL RESPONSE == "<atom>"  → атом (правило A4, без эвристик)
[0] Executor: создал hello.py, выполнил `python hello.py`
    stdout: Hello, World!
    ✓ DONE

РЕЗУЛЬТАТ [0]: Создан hello.py, вывод "Hello, World!"

МЕТРИКИ (performance §5):
  вызовов LLM: 1 (декомпозиция) + 0 (executor — детерминированные tools)
  время: ~28 с (think 950 ток / final 12 ток)
  atom_task_ratio: 1.00 (1/1)   дубликатов удалено: 0
```

Проверяемые инварианты:
- атомарность решила **модель** (ровно `<atom>`), не эвристика (AGENTS §8.1);
- Executor не вызывал LLM: файл+shell — инструменты из каталога (P4/BK-tree);
- запись в history.jsonl: одна DecompositionCall с `atomic=true`.

## 2. REST API для блога (ленивая декомпозиция + enrichment)

```bash
cascagent --query "Создать REST API для блога на FastAPI с пользователями и постами" --run
```

Ключевые шаги (полный лог — в debug.log):

```
[0]   разбита на 5: инфраструктура / модели / auth / CRUD / тесты
      (дети записаны в Kanban pending; декомпозируется только тот,
       кто начинает исполняться — ленивость)
[0.1] enrich: RAG(fastapi docs)=2, semantic=1, siblings=[] → контекст 430 ток
      разбита на 3 атома → DONE: "FastAPI + PostgreSQL настроены"
[0.2] enrich: в контекст попал результат 0.1 (чеклист ✓) + опыт blog-api
      разбита на 4 → DONE: "Модели User/Post, миграции Alembic работают"
[0.3] enrich: gaps=["актуальные практики JWT"] → ResearchAgent (isolated)
        research_cache miss → 4 итерации tools → finding (confidence 0.9)
      повторный enrich с finding → разбита на 5 (PyJWT HS256, access 15м/refresh 7д)
      DONE: "JWT авторизация работает"
[0.4] DONE: 6 endpoints, проверка прав автора
[0.5] DONE: 45 pytest passed
[0]   summarize_results: Σ len < budget → конкатенация (A12, 0 вызовов LLM)

ИТОГ: дерево 32 узла, 24 атома, research 1 (+hit на повторе формулировки),
reflect 0, вызовов LLM 47, время 14м32с, ~85k токенов.
```

Что демонстрирует сценарий: рост контекста по мере выполнения
(process.md §1), CPU-триггер исследования (process.md §5, НЕ маркер от
модели), кэш findings, сборка результата вверх без LLM.

E2e-тест фиксирует: число узлов ∈ [20..50], все leaves DONE, research ≤ 2,
`LLM-calls per root-task` < 2.0 (cpu-offload §0).

## 3. Миграция Django→FastAPI (Reflect на FAILED)

```
[0.2.3] Executor FAILED: "GenericForeignKey/through M2M не переносимы напрямую"
[0.2.3] retry ×1 (тот же промпт) → снова FAILED
[reflect] ReflectAgent (вызов 1/2 бюджета):
    FINAL: "reframe: Реализовать полиморфную ассоциацию через discriminator
           column + JSONB metadata вместо прямого переноса GenericForeignKey"
[0.2.3'] задача переформулирована (новая версия, depth+1) → PENDING → DONE
[0.2.4] sibling "Перенести Post-UserProfile M2M" при исполнении обнаруживает
         готовую полиморфную таблицу → выполняется как есть (переделка плана
         НЕ потребовалась — иллюстрация, почему REPLAN-списки не нужны)
```

⚠️ Отличие от Глав IX/XII оригинала: там Reflect возвращал `replan` со
списком cancel/new задач. В v2 контракт — `retry|reframe|give_up` одной
строкой (prompts §5, process.md §6): модель не планирует граф, это CPU.

## 4. Stripe-интеграция (Research-heavy)

Тот же сценарий, что Глава XII.4, но триггер research — пустой enrichment
по brief «Интегрировать Stripe payments» (нет локальных знаний о версии SDK):
finding содержит «stripe v7.x async, Checkout API, webhook signature
verification, idempotency keys, добавить STRIPE_SECRET_KEY»; confidence
высокая; декомпозиция даёт 6 детей. Повторный запрос «webhook stripe» →
hit по `sha256(normalize(query))` → 0 вызовов LLM (data.md §5).

## 5. Anti-примеры (чего в логах быть не должно)

| Симптом в логе | Нарушение |
|---|---|
| `[0] разбита заранее на 5×5×5 до запуска` | не ленивая декомпозиция (ADR-004) |
| `model said RESEARCH_NEEDED` | категории/маркеры вернулись в протокол (ADR-003) |
| `final_response пуст → retry "верни формат"` | парсер обязан быть толерантным; пустой = атом (A4) |
| `summarize: LLM-вызов` при Σ len < budget | A12: сначала конкатенация/сжатие |
| в history нет THINK-блока | честный парсинг обязателен (AGENTS §8.3) |
| обрезка списка детей `[:MAX_SUBTASKS]` | AGENTS §8.2: список не режем |

## 6. Открытые вопросы

- Формат `--export report.md` (product §3.1): структура Markdown-отчёта.
- Портировать ли примеры в README целиком или ссылаться сюда (Этап 6).
- Нужен ли демо-режим `--dry-run` (декомпозиция без исполнения) для
  быстрой калибровки качества разбиения без затрат на Executor.

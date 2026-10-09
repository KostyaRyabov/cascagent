# cascagent — Кэширование и производительность

> Источник: Глава VIII полной документации проекта. Бюджет одного GPU-вызова
> и профиль токенов — `stack.md §6`; что делает CPU чтобы вызовов было меньше
> — `cpu-offload.md`. Этот документ: уровни кэша, узкое место префилла,
> конфигурация движков под 4 GB VRAM, спекулятивный декодинг, метрики.

## 1. Три уровня кэша

| Уровень | Механизм | Модуль | Экономия | Доступность |
|---|---|---|---|---|
| **L1 prefix cache** | автоматический: совпадение начала промпта (SYSTEM + …) переиспользует KV-состояния | движок (llama.cpp / TabbyAPI) | ~75% времени префилла повторных запросов | оба бэкенда |
| **L2 save/restore KV** | одноразовый прогрев системного промпта, дамп/загрузка слота на диск | `cache_manager.py` (P8) | префилл промпта ~0 мс вместо ~800 мс после рестарта | **только llama.cpp** (`/slots/N?action=save|restore`); на TabbyAPI — no-op (probe при старте, stack §6.1) |
| **L3 semantic cache** | перед вызовом: lookup похожих запросов; hit ⇒ LLM не вызывается вообще | `kanban.py` exact-match (P1) + `semantic.py` recall (P2) | 100% времени повторяющихся задач | наш слой, от движка не зависит |

Порядок проверки перед любым GPU-вызовом (обязателен для всех агентов):
`L3 exact → L3 semantic (top-k в контекст) → L1/L2 (префилл дешевле) → генерация`.

Ключ L1 — дословно одинаковый `SYSTEM_PROMPT` (константа модуля, prompts §2):
любая правка текста промпта инвалидирует весь prefix cache до рестарта слота —
учитывать при ревью изменений промптов (ADR 002).

## 2. Почему префилл — узкое место

Оценка для 8B-класса, промпт ~1000 токенов, ответ ~200:

```
prompt processing: 1000 ток × ~2 мс = ~2000 мс   (29% времени вызова)
generation:         200 ток × ~25 мс = ~5000 мс
```

Отсюда приоритет контрмер:

| Способ | Экономия | Сложность | Статус |
|---|---|---|---|
| Prefix cache (авто) | 70–80% | низкая (включён) | есть, зависит от константности SYSTEM_PROMPT |
| save/restore кэша (L2) | 90–95% | средняя | `cache_manager.py`, только llama.cpp |
| Chunked prefill | 10–20% на больших ctx | низкая (флаг) | включить в конфиг (stack §4) |
| Минимальный системный промпт | пропорционально длине | низкая | сделано: ~50 токенов (prompts §7) |
| Selective Context сжатие обогащения | 30–50% токенов user-части | средняя | P5, extras-gated |

## 3. Конфигурация движка под 4 GB VRAM

Флаги TabbyAPI-кандидата (действующий llama.cpp — stack §4.1; инварианты
интерфейса для любого бэкенда — stack §4.3):

```bash
python start.py \
    --model-dir ./models/qwen3-8b-exl2 \
    --host 127.0.0.1 --port 5000 \
    --max-seq-len 8192 \
    --cache-mode Q4 \
    --gpu-split "4" \
    --chunk-size 2048
```

| Флаг | Значение | Эффект |
|---|---|---|
| `--cache-mode Q4` | квантованный KV-кэш | ~2× меньше VRAM на кэш |
| `--max-seq-len 8192` | лимит контекста | предотвращает OOM; бюджет вызова — stack §6.3 |
| `--chunk-size 2048` | chunked prefill | равномерное потребление VRAM при длинном промпте |
| `--gpu-split "4"` | вся карта | использует все 4 GB |

## 4. Draft model (спекулятивный декодинг)

Принцип: маленькая draft-модель предсказывает N токенов, большая проверяет их
одним pass'ом; принятые токены удешевляют генерацию.

```bash
python start.py \
    --model-dir ./models/qwen3-4b-exl2 \
    --draft-model-dir ./models/qwen3-1.7b-exl2 \
    --num-speculative-tokens 5
```

Ориентировка: 4B без draft ~55–75 tok/s → с draft ~80–120 tok/s (1.5–2×).
VRAM-ограничение: main+draft должны влезть вместе —
`4B + 1.7B ≈ 3.5 GB ✅`, `8B + 1.7B ≈ 5.5 GB ❌`.

**Компромисс стека (связан с ADR 001):** качество ⇒ 8B без draft; скорость ⇒
4B + draft. Текущее решение AGENTS.md §3 — 4B **без** draft (speculative
decoding вне скоупа v0.x, AGENTS.md §9 «не делать»); если Этап 2 подтвердит
таргеты по скорости на 4B, draft остаётся зарегистрированной оптимизацией
(cpu-offload §7), не задачей.

## 5. Метрики (`PerformanceMetrics`)

Собираются оркестратором по окончании сессии из `history.jsonl` + таймингов
канбанa; вывод — `cascagent report` (product §5.4). Сырьё пишется на каждом
вызове (cpu-offload A13, data.md §3 DecompositionCall).

```python
@dataclass
class PerformanceMetrics:
    # время
    total_wall_time_ms: int
    total_llm_time_ms: int
    avg_prompt_processing_ms: float
    avg_generation_ms: float
    # токены
    total_tokens_input: int
    total_tokens_output: int
    avg_tokens_per_call: float
    # кэш
    prefix_cache_hits: int
    prefix_cache_misses: int
    semantic_cache_hits: int
    # эффективность декомпозиции
    duplicates_removed: int
    avg_calls_per_task: float
    atom_task_ratio: float      # доля атомарных задач (калибровка порога detector)
```

Сводка в CLI (минимум для принятия решений):

```python
def print_metrics_summary(m: PerformanceMetrics) -> None:
    print(f"Общее время: {m.total_wall_time_ms/1000:.1f}с")
    print(f"LLM время:   {m.total_llm_time_ms/1000:.1f}с")
    hits = m.prefix_cache_hits + m.prefix_cache_misses
    if hits:
        print(f"Prefix cache hit rate: {m.prefix_cache_hits/hits*100:.1f}%")
    print(f"Semantic cache hits: {m.semantic_cache_hits}")
    print(f"Атомарных задач: {m.atom_task_ratio*100:.1f}%")
    print(f"Дубликатов удалено: {m.duplicates_removed}")
    print(f"LLM-calls per root-task: {m.avg_calls_per_task:.2f}")
```

Интерпретация (связка с таргетами product §7):
- `total_llm_time / total_wall_time` → сколько съедает GPU; много ⇒ искать
  промахи L3 и лишние ретраи;
- низкий prefix-cache hit ⇒ кто-то ломает константность SYSTEM_PROMPT;
- `atom_task_ratio` вне коридора 30–60% ⇒ калибровать промпт/порог 0.75;
- `duplicates_removed` большой ⇒ модель зацикливается, проверить depth-политику think.

## 6. Открытые вопросы

- Замерять ли prefix-cache hit rate напрямую (llama.cpp `/metrics`) или только
  косвенно по `prompt_processing_ms` — решить при Этапе 2 ревизии стека.
- L3 semantic cache сейчас = recall top-k в контекст; автоответ при
  similarity ≥ 0.95 без вызова модели — включить или нет (риск устаревания
  опыта при изменении проекта).

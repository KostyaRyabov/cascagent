# cascagent — Эксплуатация: железо, запуск, диагностика, troubleshooting

> Статус: **проект v2**. Детализация Главы XI полной документации.
> ⚠️ Глава XI оригинала описывала стек TabbyAPI + Qwen3 8B EXL2; действующее
> решение кодовой базы — **llama.cpp + Qwen3 4B Q4_K_M** (ADR-001 Accepted,
> stack §1.4). Здесь зафиксированы оба варианта; кандидатский помечен.
> Конфигурация `config.toml` — `docs/config.md`; бюджет токенов — stack §6.

## 1. Целевое железо

| Компонент | Минимум | Рекомендуется | Комментарий |
|---|---|---|---|
| GPU | NVIDIA 4 GB VRAM (GTX 1650 / 1650 Ti) | 8 GB+ | 4 GB ⇒ только 4B GGUF или 8B EXL2 |
| CPU | 4 ядра | 6–8 ядер | `--threads 6` референс; CPU-fallback инференса |
| RAM | 8 GB | 16 GB | numpy-перебор semantic recall живёт в RAM |
| Storage | 20 GB | 50 GB | модели ~3–5 GB + KV-дампы `.bin` + data/ |
| OS | Linux/macOS/Windows | Linux | пути в config — относительные (`./data/`) |

Референсная конфигурация замеров: GTX 1650 Ti 4GB / Ryzen 5 4600H / 16 GB / NVMe.

## 2. Ожидаемая производительность (capacity planning)

Цифры — порядок величин для планирования, не бенчмарк; честные значения —
из `history.jsonl` после Этапа 2 (stack §6.2, performance §5).

Действующий стек (llama.cpp, Qwen3 4B Q4_K_M): генерация ~15–25 tok/s,
префилл ~100–200 tok/с (cpu-offload §0). Кандидат (TabbyAPI, 8B EXL2):
~40–55 tok/s без draft.

| Сценарарий | Вызовы LLM | Ориентир времени |
|---|---|---|
| Hello World (1 атом) | 1 | ~30 с |
| REST API (~30 узлов) | ~47 | 15–30 мин на 4B (4B медленнее 8B, чаще retry) |
| Сложная система + research | 60+ | 30–90 мин |

Дисциплина: время растёт линейно по `LLM-calls per root-task` — метрика и
контрмеры (P1–P9) в cpu-offload §0/§2.1.

## 3. Установка и запуск

### 3.1 llama.cpp (действующий бэкенд)

```bash
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build -DGGML_CUDA=ON && cmake --build build --config Release -j
# модель: HF Qwen/Qwen3-4B-GGUF (Q4_K_M ~3.0 GB) или qwen3-4b-q4_k_m.gguf
./build/bin/llama-server --model ~/models/qwen3-4b-q4_k_m.gguf \
  --host 127.0.0.1 --port 8080 --n-gpu-layers 999 --ctx-size 8192 \
  --flash-attn --cache-type-k q8_0 --cache-type-v q8_0 \
  --no-mmap --threads 6
```

Проверка: `curl http://127.0.0.1:8080/v1/models` → JSON со списком моделей.

### 3.2 TabbyAPI (кандидат ADR-001 Proposed — не менять код до принятия!)

```bash
git clone https://github.com/theroyallab/tabbyAPI && cd tabbyAPI
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
huggingface-cli download LoneStriker/Qwen3-8B-4.0bpw-h6-exl2 \
    --local-dir ./models/qwen3-8b-exl2
python start.py --model-dir ./models/qwen3-8b-exl2 \
    --host 127.0.0.1 --port 5000 --max-seq-len 8192 \
    --cache-mode Q4 --gpu-split "4"
```

Последствия для cascagent при переходе: `cache_manager.py` → no-op (probe
`/slots` при старте, лог degraded mode); think через `/no_think`; `client.py`
адаптируется конфигом `[llm] backend = "tabbyapi"` (инварианты интерфейса —
stack §4.3). Никаких изменений в decomposer/enricher/orchestrator.

### 3.3 cascagent

```bash
pip install cascagent                    # релиз
# разработка:
git clone <repo> && cd cascagent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                  # extras — config.md §2
cascagent --query "Написать Hello World на Python"
```

## 4. Конфигурация под разное железо

Базовый формат — `docs/config.md §4`. Дельты профилей:

| Профиль | llm.url | Модель | max_tokens | think_max_depth | ctx |
|---|---|---|---|---|---|
| 4 GB VRAM (референс) | :8080 | Qwen3 4B Q4_K_M | 4000 | 2 | 8192 |
| 8 GB+ VRAM | :5000 | 8B EXL2 (кандидат) | 8000 | 3 | 16384 |
| CPU-only fallback | :8080 | Qwen3 1.7B Q4 | 2000 | 1 | 4096 |

CPU-only замечания: префилл ~10–20 ток/с ⇒ декомпозиция узла 2–5 мин;
рекомендуется `enrichment.max_context_tokens = 300` и `research.web_search_enabled=false`.

## 5. Мониторинг и диагностика

Артефакты (все пути — `[storage]`, config §4):
- `data/history.jsonl` — по строке на LLM-вызов (DecompositionCall, data §3);
- `data/debug.log` — человекочитаемый, секции `--- THINK --- / --- FINAL RESPONSE ---`, grep по task_id; ротация по размеру;
- `data/kanban.db` — SQLite WAL; `sqlite3 data/kanban.db "SELECT status,count(*) FROM tasks GROUP BY status;"`;
- `data/backups/kanban-<ts>.db` — periodic backup (data §4.4).

Планируемые команды CLI (Этап 3; сверить с product §2.5 S3):

```bash
cascagent status            # счётчики: всего/DONE/RUNNING/ENRICHMENT/pending, вызовы LLM, время
cascagent tree              # дерево задач со статусами ✓/✗/…
cascagent report            # PerformanceMetrics (performance §5)
cascagent health            # см. ниже
```

Ожидаемый вывод `health`:

```
✓ llama.cpp доступен (:8080/v1/models)
✓ модель загружена: qwen3-4b-q4_k_m
✓ VRAM: 3.2/4.0 GB   (nvidia-smi, если доступен)
✓ SQLite WAL OK, tasks=45
⚠ prefix cache hit rate 61% (< таргета 80%) — проверить константность SYSTEM_PROMPT
```

Метрики health берутся из history.jsonl + `/metrics` движка (открытый
вопрос performance §6 — прямой или косвенный замер hit rate).

## 6. Troubleshooting

| Симптом | Диагноз | Действие |
|---|---|---|
| CUDA out of memory | модель+KV не влезают | `--ctx-size 4096`; квант KV `q4_0`; меньшая модель; проверить `--no-mmap` |
| Connection refused :8080 | сервер не запущен/упал | `curl /v1/models`; журнал llama-server; systemd unit? (вне скоупа) |
| Медленная генерация | offload на CPU неполный | `nvidia-smi` — доля слоёв на GPU; поднять `--n-gpu-layers`; закрыть прочее GPU-приложение |
| Пустые final_response в debug.log | обрезка по max_tokens внутри think | увеличить `llm.max_tokens`; проверить что degenerate-правило не маскирует тримминг (логируется отдельно) |
| Модель зацикливается на детях | слабый reasoning на глубине | включить think глубже (`think_max_depth=3`); проверить threshold 0.75 (atom_task_ratio вне 30–60% — performance §5) |
| Много дублей в дереве | промпт/модель деградировали | смотреть `duplicates_removed` в history; калибровать порог detector; НЕ резать список (AGENTS §8.2) |
| Разросшийся semantic_memory | нет GC | `forget_old(days=30, max_keep=1000)` (algorithms §3) |
| Kanban «застрял» на RUNNING/ENRICHMENT | краш посреди вызова/обогащения | resume: RUNNING и ENRICHMENT →PENDING при старте (process §7); DONE не перевыполняется |
| Префикс-кэш не бьёт | кто-то правит SYSTEM_PROMPT динамически | константа модуля (prompts §2); тест снапшот текста |

## 7. Безопасность и приватность

- Локальный инференс: данные не покидают машину; `web_search_enabled=false`
  по умолчанию (config §4) — интернет только по явному флагу.
- Executor sandbox: рабочая директория проекта + allowlist инструментов;
  destructive операции — только `--auto-approve all` либо подтверждение
  (product §3.2). По умолчанию `safe`.
- Логирование полных prompt/result в history — это проектные артефакты
  отладки; перед публикацией репозитория чистить `data/` (.gitignore уже
  исключает `data/`).

## 8. Открытые вопросы

- systemd/Docker unit для llama-server — включать ли в scripts/ (пока нет).
- Ротация debug.log: по размеру (10 MB?) — зафиксировать в history.py.
- Замер VRAM через NVML (`pynvml`) vs парсинг nvidia-smi — выбрать на Этапе 2.

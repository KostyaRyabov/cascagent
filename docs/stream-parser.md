# Stream Parser — модель данных (draft, 2026-10-11)

Статус: **проектирование, код не написан.** Это рабочие модели сервиса
`StreamParser`, который встраивается в `Agent` (НЕ плагин;
`OutputSplitterPlugin` упразднён). Парсер принимает сырые SSE-чанки
llama.cpp (`delta.content`) и приводит их к дискретным «единицам»
(слова / спец-токены), которые передаются в `RuntimePlugin.on_upload`.

---

## 1. Итоговые модели

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class UnitStatus(str, Enum):
    """Насколько токен(ы) текущей итерации сформировали единицу."""
    PARTIAL = "partial"   # слово ещё не дописано (в буфере префикс)
    WORD    = "word"      # слово закрыто (граница: пробел/пункт/спец)
    SPECIAL = "special"   # распознан служебный тег из whitelist


class SegmentKind(str, Enum):
    """Типы сегментов. Вне тегов — kind=None (см. §3, п.1)."""
    THINK     = "think"
    TOOL_CALL = "tool_call"
    # расширяется whitelist'ом конфига


@dataclass
class Segment:
    """Узел дерева вывода. Текст пополняется ТОЛЬКО подтверждёнными
    юнитами (все RuntimePlugin дали continue) — см. §4."""
    kind: Optional[SegmentKind] = None   # None == вне тегов ("response")
    text: str = ""                       # подтверждённый накопленный текст
    children: List["Segment"] = field(default_factory=list)
    open: bool = True                    # False после закрытия тега/неявного
    truncated: bool = False              # закрыт неявно (обрыв, tool_call)
    is_open_tag: Optional[bool] = None   # self-closing/void-тег → False;
                                         # None — обычный парный (опц.)


@dataclass
class Unit:
    """Единица, отдаваемая наружу (в on_upload). Атомарна для одной
    итерации стрима."""
    status: UnitStatus
    word: str          # special → имя тега ("think", "/think");
                       # partial → незакрытый хвост буфера
    raw: str           # исходные куски чанков, породившие юнит
    segment: Segment   # ССЫЛКА на текущий сегмент (для обогащения)


@dataclass
class ParseState:
    """Всё состояние парсера. Хранится НЕ в Memory, а как состояние
    сервиса; в плагины попадает через аргумент `data`."""
    buffer: str = ""                     # единый буфер-строка (§5)
    current: Optional[Segment] = None    # активный лист (push/pop по стеку)
    root: Optional[Segment] = None       # корень дерева; root.children —
                                         # готовые верхнеуровневые сегменты
    stack: List[Segment] = field(default_factory=list)
    finished: bool = False               # finish_reason / [DONE] получен
    finish_reason: Optional[str] = None  # stop | length | tool_calls | ...
```

---

## 2. RuntimePlugin: новая сигнатура hot-path

```python
class RuntimePlugin(Plugin):
    name = "base_runtime"

    def on_upload(self, unit: Unit, data: ParseState) -> Dict[str, Any]:
        """Вызывается на КАЖДЫЙ готовый юнит (word/special), а не на
        каждый сырой токен. Частичные юниты (PARTIAL) plugins не видят.

        Возврат: {"op": "continue" | "stop" | "error", "data": ...}
        (семантика op не меняется — see plugins.md §6.3).

        Только если ВСЕ RuntimePlugin вернули continue, парсер делает
        commit: unit.word += " " дописывается в unit.segment.text (§4)."""
        return {"op": "continue"}
```

Порядок вызова внутри `Agent.run()`:

```
SSE chunk → StreamParser.feed(chunk) → [Unit?] → all on_upload(unit, state)
                                                      ├─ все continue → commit в Segment
                                                      └─ кто-то stop/error → break потока
```

---

## 3. Ключевые решения раунда 2026-10-11 (закрытые вопросы)

1. **`response` — это не сегмент-тег**, а состояние «вне тегов»:
   `kind=None`. Никакого тега `<response>` в потоке нет.
2. **Только подтверждённые состояния** пишутся в `Segment.text`;
   `segment_path: Tuple[int, ...]` удалён — вместо него прямая ссылка
   `Unit.segment`.
3. **`root` оставлен**: без него дерево сегментов не собрать (closed
   сегменты должны куда-то ложиться). Но он живёт в `ParseState`,
   а не в юнитах.
4. **Буфер один**: `buffer: str` (Deque убран). Пунктуация отдельной
   переменной не выделяется — она лишь закрывает слово.
   `pending_word` / `pending_marker` убраны: маркеры ищутся прямо в
   `buffer` (суффикс-префикс сравнение с whitelist).
5. **`units: List[Unit]` убран** — юниты не накапливаются, они
   одноразовые сообщения; вся персистентность только в дереве
   `Segment`.
6. **BKTree + Левенштейн** — для fuzzy-распознавания тегов с опечатками
   (`<thnk>`, `<think`); словарь whitelist строится при init агента.
7. **tool_call внутри think** — парсер принудительно закрывает think
   (`truncated=True`), извлекает payload, агент завершает итерацию и
   запускает новый `run()`; незакрытый `</think>` не ожидается.
8. **Размер буфера**: `max_buffer_tokens` (кол-во сырых чанков) — конфиг
   `[parser]` секция; переполнение → flush как WORD.

---

## 4. Протокол коммита (state machine)

```
feed(raw_chunk):
  buffer += raw_chunk
  loop:
    if buffer суффиксом == открывающий тег whitelist (или fuzzy ≤ k):
        flush_как_word(); emit Unit(SPECIAL, tag_name)
        push/pop Segment (stack)
    elif buffer содержит границу слова (пробел/пункт/换行):
        emit Unit(WORD, word)  → on_upload всем → commit?
    else:
        emit Unit(PARTIAL)? NO — молчим, ждём продолжения
  if finish_reason:
    flush остаток buffer как WORD; закрыть все open сегменты truncated=True
```

Commit-правило: `segment.text += unit.word (+ " ")` выполняется
парсером **после** того, как все `on_upload` вернули `continue`.

---

## 5. Открытые вопросы (следующий раунд)

- Как показывать пользователю уже выведенное слово, если later plugin
  дал `stop` (edit of rendered output vs просто cut)?
- Нужен ли `unit.raw` вообще, или достаточно `word`?
- Вложенность: максимальная глубина стека (config?) и правило:
  `tool_call` не может содержать `think`?

# Модель парсера стрима LLM (итоговая, согласованная)

Статус: обсуждение/проектирование — в коде ещё не реализовано.

## Раунд правок v2

**1. Что означало `source`?** Различало способ появления сегмента: "tag" — создан явными тегами, "outside" — текст «вне тегов» (обычный вывод модели, который по аналогии называли response). Поле убрано: ту же информацию несёт `kind` — сегмент `RESPONSE` без тега-открывателя и есть «вне тегов». Если понадобится жёсткое различие — вернём как вычисляемое свойство, не поле состояния.

**2. `open_tag` / `is_open_tag` убраны.** Какой тег открыл сегмент уже закодирован в `kind`. Сырой вид тега с опечаткой (`<thnk>`) — деталь распознавания, а не структуры: она живёт в момент парсинга в `ParseState.word` при `status=SPECIAL`. Дублировать её в каждом сегменте незачем.

**3. Подтверждение создания сегмента = подтверждению слова.** Открывающий тег порождает сегмент НЕ сразу, а по тому же контракту, что и слово: SPECIAL(открывающий тег) -> все RuntimePlugin дали continue -> Segment создан и добавлен в segments + stack; хоть один дал stop/error -> тег считается обычным текстом, сегмент НЕ создаётся. В момент вызова on_upload для открывающего тега сегмента может ещё не существовать — плагин голосует за его рождение. Аналогично закрывающий тег голосует за pop() из стека.

**4. `pending_marker` убран из ParseState.** Буфер `buffer` — единственный аккумулятор сырых токенов. Если в его конце лежит префикс возможного тега ("<thi"), он никуда не исчезает: слово остаётся PARTIAL, пока не станет ясно — замкнулось ли слово (текст) или собрался тег (SPECIAL). Неопределённость на границе чанков разрешается статусом PARTIAL + флагом done/finish_reason (на [DONE] незакрытый префикс дописываем как слово). Список возможных тегов — whitelist парсера (конфиг), не состояние.

## Итоговая модель

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ParseStatus(str, Enum):
    """Насколько токены текущей итерации сформировали элемент."""
    PARTIAL = "partial"   # элемент ещё не дописан — ждём следующие чанки
    WORD = "word"         # слово явно замкнуто (пробел/пунктуация/граница сообщения)
    SPECIAL = "special"   # распознан служебный тег из whitelist (think, tool_call, ...)


class SegmentKind(str, Enum):
    THINK = "think"
    RESPONSE = "response"  # по факту — состояние «вне тегов»
    TOOL_CALL = "tool_call"
    # ... расширяется whitelist'ом


@dataclass
class Segment:
    kind: SegmentKind
    text: str = ""                        # только ПОДТВЕРЖДЁННЫЕ word/special
    finish_reason: Optional[str] = None   # None = открыт; "stop" | "length" | "aborted" | "unclosed"
    segments: List["Segment"] = field(default_factory=list)  # подсегменты (иерархия)


@dataclass
class ParseState:
    """Единственное ТЕКУЩЕЕ состояние парсера."""

    # --- статус текущего элемента ---
    status: ParseStatus = ParseStatus.PARTIAL
    word: Optional[str] = None            # заполняется только при WORD / SPECIAL

    # --- реконструкция слов ---
    buffer: str = ""                      # просто строка недособранных токенов;
                                          # слово готово -> word = buffer; buffer = ""

    # --- сегменты ---
    segments: List[Segment] = field(default_factory=list)  # все собранные сегменты
    stack: List[Segment] = field(default_factory=list)     # стек ОТКРЫТЫХ сегментов;
                                          # stack[-1] — текущий; закрывающий тег -> pop() -> родитель всплывает

    # --- поток / протокол ---
    finish_reason: Optional[str] = None   # "stop" | "length" | "tool_calls" | None
    done: bool = False                    # получен [DONE]
```

**Финальная правка (раунд v3): `finish_reason` убран из `ParseStatus`.**
Он есть у каждого `Segment` (как сегмент завершился) и остаётся в `ParseState` только как
протокольное поле потока (почему остановилась генерация LLM). Дублировать его в статусе
текущего элемента незачем — они могут не совпадать (поток закончился по "length",
а открытый сегмент будет закрыт с "unclosed"). В `ParseStatus` остаются только
`PARTIAL` / `WORD` / `SPECIAL`.

## Что убрали по ходу обсуждения
- `Unit` — его поля (`status`, `word`) переехали прямо в `ParseState`; состояние единичное и текущее.
- `raw: str` — дублировал `buffer`.
- `max_buffer_tokens` — настройка парсера, не состояние.
- `segment_path: Tuple[int, ...]` — навигация через `stack`.
- `root: Segment` — заменён на плоский сбор `segments` + `stack` ссылок.
- `Deque[str] buffer` + `pending_word` + `pending_marker` — оставлен один строковый `buffer`.
- `source`, `open_tag`, `is_open_tag` — информация дублировала `kind`.
- `truncated: bool` — заменён на `finish_reason` по аналогии с выводом LLM.
- `children` -> `segments` (единая терминология: сегменты и подсегменты).
- `finish_reason` из `ParseStatus` — дублировал `Segment.finish_reason` и протокольный `ParseState.finish_reason`.

## Интерфейс плагина

```python
class RuntimePlugin:
    def on_upload(self, state: ParseState) -> Dict[str, Any]:
        """Вызывается когда элемент готов (WORD/SPECIAL).
        Возврат: {"op": "continue" | "stop" | "error", ...}.
        Слово попадает в stack[-1].text, а special-элемент создаёт/закрывает сегмент
        ТОЛЬКО если все плагины дали continue."""
```

## Правила, зафиксированные в обсуждении
1. Пунктуация не отдельный тип — она лишь замыкает слово в `word`.
2. Сегменты = блоки тегов; `RESPONSE` — фактически «вне тегов».
3. Вложенность допустима (whitelist + ограничение глубины); закрытие толерантное: несоответствующий закрывающий тег игнорируется как шум, неявное закрытие предков чинит пропущенные теги.
4. `tool_call` внутри `think` = аномалия: принудительно закрываем think (`finish_reason="aborted"`), извлекаем payload, новая итерация `run()`; оборванный `think` сохраняем для отладки.
5. Одиночные/self-closing теги не двигают стек.
6. Опечатки в тегах: BKTree + Левенштейн по whitelist.
7. Сегмент рождается и закрывается тем же голосованием плагинов, что и слово.
8. На `[DONE]`/`finish_reason` все открытые сегменты закрываются с `finish_reason="unclosed"`, остаток `buffer` дописывается как слово.

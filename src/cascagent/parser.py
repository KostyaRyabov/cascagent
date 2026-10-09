"""Parsing of raw LLM output into structured subtasks (protocol v2).

CPU-side responsibility (AGENTS.md §2.3): the model answers in plain text
(brief line + indented description lines); all structure extraction
happens here deterministically. The model knows nothing about categories,
dedup rules or storage — see docs/protocol.md.

Protocol invariants (docs/protocol.md §3):
- atomic task == exactly one token ``<atom>`` (case-insensitive, after
  sanitize) OR an empty final response — no heuristics (§9.1 AGENTS.md);
- a line WITHOUT leading whitespace starts a new subtask (``brief``);
- INDENTED lines are the current subtask's description, joined with single
  spaces into one ``description``;
- we NEVER truncate the list (filtering is DuplicateDetector's job upstream);
- preamble before the first brief line is ignored; no valid pair is lost.

``SYSTEM_PROMPT`` is frozen verbatim (protocol §2): it is the stable prefix
that llama.cpp prefix-caching / KV save-restore relies on. Change it only
together with the tests.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from .models import Task

# --------------------------------------------------------------------------
# System prompt — FROZEN VERBATIM (docs/protocol.md §2). ~200 chars.
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """\
Разбей задачу на подзадачи следующего уровня детализации.

КАЖДАЯ ПОДЗАДАЧА:
Название задачи
    Подробное описание что нужно сделать

ПРАВИЛА:
- Подзадачи должны полностью покрывать исходную задачу
- Каждая подзадача — конкретное действие
- Не повторяй исходную задачу
- Если задача не делится — напиши только: <atom>\
"""

#: Atomic marker — exact match (case-insensitive) is the ONLY textual
#: atomicity signal the parser accepts (principle AGENTS.md §9.1).
ATOM_MARKER = "<atom>"

# Think-block markers used by Qwen3 / llama.cpp chat templates.
# Opening tag may carry attributes: <think attribute="...">
THINK_OPEN_RE = re.compile(r"<\s*think\b[^>]*>", re.IGNORECASE)
THINK_CLOSE_RE = re.compile(r"<\s*/\s*think\s*>", re.IGNORECASE)

# Leading junk we strip from a line (numbering / bullets / markdown).
_BULLET_PREFIX_RE = re.compile(
    r"^\s*(?:[-*\u2022\u25cf\u25e6]|\d{1,3}[.)\]]|#{1,6})\s*"
)
# Bold/italic/code markdown wrappers around the whole line.
_MD_WRAP_RE = re.compile(r"^[\s`*_]+|[\s`*_]+$")
# Markdown emphasis markers glued to text inside a line: **main.py** -> main.py
_EMPHASIS_RE = re.compile(r"\*{1,3}|_{2,3}|`+")
# Zero-width and BOM noise.
_NOISE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def split_think_and_response(raw: str) -> Tuple[str, str]:
    """Split raw completion into (think, final_response).

    Handles:
    - complete block:      \" ...thinking... \"
    - closed-only remnant: \" ...thinking... \"  (rare, treat as think)
    - unclosed block:      "" + thinking (model truncated) ->
      everything after the open tag is think, response is empty
    - no tags at all:      ("", raw)

    Never raises. Returns strings stripped of leading/trailing whitespace.
    """
    if not raw:
        return "", ""

    m_open = THINK_OPEN_RE.search(raw)
    m_close = THINK_CLOSE_RE.search(raw)

    if m_open is None and m_close is None:
        return "", raw.strip()

    if m_open is not None and m_close is not None and m_close.start() > m_open.end():
        think = raw[m_open.end(): m_close.start()]
        response = raw[m_close.end():]
        # A second (hallucinated) think block inside the response area:
        # cut it off — response ends where the next block starts.
        extra = THINK_OPEN_RE.search(response)
        if extra:
            response = response[: extra.start()]
        return think.strip(), response.strip()

    if m_open is not None:
        # Unclosed think: treat tail as still-thinking, no final answer yet.
        prefix = raw[: m_open.start()]
        think = raw[m_open.end():]
        return think.strip(), prefix.strip()

    # Only a closing tag present: content before it was the think block.
    think = raw[: m_close.start()]
    response = raw[m_close.end():]
    return think.strip(), response.strip()


def sanitize_line(line: str) -> str:
    """Clean one raw output line: strip markdown, numbering, quotes, noise.

    Deterministic typo/format repair only — no semantic changes.
    Applied repeatedly (prefix junk <-> emphasis wrappers interleave,
    e.g. ``**- 1. Создать main.py**``).
    """
    if not line:
        return ""
    s = _NOISE_RE.sub("", line)
    s = s.strip()
    for _ in range(4):
        prev = s
        # Whole-line wrappers: **Создать main.py** -> Создать main.py
        s = _MD_WRAP_RE.sub("", s)
        # List/numbering artifacts: "- 1. Создать" -> "Создать"
        s = _BULLET_PREFIX_RE.sub("", s)
        # Emphasis markers glued inside the text: main.**py** -> main.py
        s = _EMPHASIS_RE.sub("", s)
        s = re.sub(r"\s+", " ", s).strip()
        if s == prev:
            break
    return s


def is_atomic(final_response: str) -> bool:
    """True iff the response is exactly the atom marker or empty.

    Exact-match rule (protocol §3, invariant 1; AGENTS.md §9.1):
    NO length/content heuristics. ``<atom>`` survives surrounding
    whitespace and markdown wrappers thanks to sanitize_line, and is
    compared case-insensitively. Post-dedup atomarity (rule R1) is a
    separate decision made by the orchestrator, not here.
    """
    if not final_response or not final_response.strip():
        return True
    return sanitize_line(final_response).lower().rstrip(".:") == ATOM_MARKER


def parse_decomposition(final_response: str, parent_id: Optional[str] = None,
                        depth: int = 0) -> List[Task]:
    """Parse FINAL RESPONSE text into a list of subtask ``Task`` nodes.

    Indentation protocol (docs/protocol.md §3):
    - non-indented sanitized line => new ``brief``;
    - indented lines => appended to current ``description`` (joined by ' ');
    - text before the first brief line (preamble) is ignored;
    - ``<atom>`` / empty response => [] (caller checks :func:`is_atomic`).

    Sanitize preserves per-line indentation intent: we detect indentation
    on the RAW line (leading whitespace), then clean the content.
    Never truncates legitimate output (§9.2 AGENTS.md).
    """
    if not final_response or not final_response.strip():
        return []
    if is_atomic(final_response):
        return []

    tasks: List[Task] = []
    current_brief: Optional[str] = None
    current_ctx: List[str] = []

    def _flush() -> None:
        if current_brief is not None:
            tasks.append(Task(
                brief=current_brief,
                description=" ".join(current_ctx),
                depth=depth,
                parent_id=parent_id,
            ))

    for raw_line in final_response.splitlines():
        if not raw_line.strip():
            continue  # blank separator between subtasks
        indented = raw_line[0].isspace()
        cleaned = sanitize_line(raw_line)
        if not cleaned:
            continue
        if cleaned.lower().rstrip(".:") == ATOM_MARKER:
            # Atom marker inside a longer answer: the model contradicted
            # itself. Honest parsing: atom wins only when it IS the whole
            # response (checked above); mid-list occurrence is dropped.
            continue
        if not indented:
            _flush()
            current_brief, current_ctx = cleaned, []
        elif current_brief is not None:
            current_ctx.append(cleaned)
        # indented line with no brief yet => preamble noise, ignore

    _flush()
    return tasks


def build_user_prompt(task_brief: str,
                      context_lines: Optional[Sequence[str]] = None,
                      completed_siblings: Optional[Sequence[str]] = None) -> str:
    """Compose the minimal USER prompt for one isolated decomposition call.

    Reference layout (docs/protocol.md §5)::

        Задача: {brief}

        Контекст:
        {enriched_context}          # assembled by ContextEnricher (CPU)

        Выполненные ранее:
        - {sibling} ✓               # completed siblings checklist

        РАЗБЕЙ НА ПОДЗАДАЧИ:

    Pure function — testable without network. The model never learns where
    context came from (RAG / semantic memory / siblings are system internals).
    """
    parts: List[str] = [f"Задача: {task_brief.strip()}"]

    ctx = [c.strip() for c in (context_lines or []) if c and c.strip()]
    if ctx:
        parts.append("Контекст:\n" + "\n".join(ctx))

    done = [s.strip() for s in (completed_siblings or []) if s and s.strip()]
    if done:
        parts.append("Выполненные ранее:\n"
                     + "\n".join(f"- {s} \u2713" for s in done))

    parts.append("РАЗБЕЙ НА ПОДЗАДАЧИ:")
    return "\n\n".join(parts)

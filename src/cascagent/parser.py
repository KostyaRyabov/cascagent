"""Parsing of raw LLM output into structured subtasks.

CPU-side responsibility (see AGENTS.md section 2.3): the model answers in
plain text; all structure extraction happens here deterministically.

Protocol (AGENTS.md section 4):
- each line is one subtask starting with '>', '!' or '?'
- an atomic task is exactly one line: '<atom>'
- forbidden in output: numbering, markdown, tags, explanations --
  the parser *tolerates* them (sanitize), but never invents tasks.

Atomicity rule (section 9.1): NO heuristics -- only exact '<atom>' match
or empty final response counts as atomic.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .models import SYMBOL_TO_CATEGORY, Task, TaskCategory

# Think-block markers used by Qwen3 / llama.cpp chat templates.
# Opening tag may carry attributes: <think attribute="...">
THINK_OPEN_RE = re.compile(r"<\s*think\b[^>]*>", re.IGNORECASE)
THINK_CLOSE_RE = re.compile(r"<\s*/\s*think\s*>", re.IGNORECASE)

ATOM_MARKER = "<atom>"

# Leading junk we strip from a subtask line (numbering / bullets / markdown).
_BULLET_PREFIX_RE = re.compile(
    r"^\s*(?:[-*\u2022\u25cf\u25e6]|\d{1,3}[.)\]]|#{1,6})\s*"
)
# Bold/italic/code markdown wrappers around the whole line.
_MD_WRAP_RE = re.compile(r"^[\s`*_]+|[\s`*_]+$")
# Markdown emphasis markers glued to text inside a title: **main.py** -> main.py
_EMPHASIS_RE = re.compile(r"\*{1,3}|_{2,3}|`+")
# Zero-width and BOM noise.
_NOISE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def split_think_and_response(raw: str) -> Tuple[str, str]:
    """Split raw completion into (think, final_response).

    Handles:
    - complete block:      " ...thinking... "
    - closed-only remnant: " ...thinking... "  (rare, treat as think)
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

    Deterministic typo/format repair only -- no semantic changes.
    Applied repeatedly (prefix junk <-> emphasis wrappers interleave,
    e.g. ``- **1. ! task**``).
    """
    if not line:
        return ""
    s = _NOISE_RE.sub("", line)
    s = s.strip()
    for _ in range(4):
        prev = s
        # Whole-line wrappers: **! task** -> ! task
        s = _MD_WRAP_RE.sub("", s)
        # List/numbering artifacts: "- 1. ! task" -> "! task"
        s = _BULLET_PREFIX_RE.sub("", s)
        # Emphasis markers glued inside the text: main.**py** -> main.py
        s = _EMPHASIS_RE.sub("", s)
        s = re.sub(r"\s+", " ", s).strip()
        if s == prev:
            break
    return s


def detect_category(line: str) -> Tuple[Optional[TaskCategory], str]:
    """Detect protocol category of a sanitized line.

    Returns (category, title_without_symbol). Category is None when the
    line carries no protocol symbol. Exact '<atom>' -> (ATOM, "").
    """
    s = line.strip()
    if not s:
        return None, ""
    low = s.lower()
    if low == ATOM_MARKER or low.rstrip(".:") == ATOM_MARKER:
        return TaskCategory.ATOM, ""
    sym = s[0]
    cat = SYMBOL_TO_CATEGORY.get(sym)
    if cat is None:
        return None, s
    title = s[1:].strip()
    return cat, title


def parse_response(final_response: str, parent_id: Optional[str] = None,
                   depth: int = 0) -> List[Task]:
    """Parse the FINAL RESPONSE text into a list of subtask ``Task`` nodes.

    Rules (honest parsing, section 9):
    - We NEVER truncate the list beyond duplicate filtering done upstream
      by DuplicateDetector (the detector is applied by the decomposer).
    - Lines without a protocol symbol are ignored (explanations/hallucinations).
    - '<atom>' anywhere as its own line => exactly one ATOM task returned.
    - Empty/garbage response => empty list (caller treats as atomic via
      DecompositionCall.is_atomic on exact-match/empty only).
    """
    if not final_response or not final_response.strip():
        return []

    tasks: List[Task] = []
    for raw_line in final_response.splitlines():
        line = sanitize_line(raw_line)
        if not line:
            continue
        cat, title = detect_category(line)
        if cat is None:
            continue  # explanation line, ignore
        if cat is TaskCategory.ATOM:
            # Atomic marker wins, exactly one atom task (protocol: single line).
            return [Task(title=ATOM_MARKER, category=TaskCategory.ATOM,
                         depth=depth, parent_id=parent_id, status="atomic")]
        if not title:
            continue  # symbol with no text -- nothing actionable
        tasks.append(Task(title=title, category=cat, depth=depth,
                          parent_id=parent_id,
                          status="pending"))
    return tasks


def build_decomposition_prompt(task_title: str, category: TaskCategory,
                               parent_title: Optional[str],
                               completed_siblings: Optional[List[str]] = None,
                               research_results: Optional[List[Tuple[str, str]]] = None) -> str:
    """Compose the minimal user-prompt for one isolated decomposition call.

    Context policy (AGENTS.md section 5):
    - parent context = ONLY the parent's title (it is already a summary);
    - completed siblings passed as a check-list;
    - research results appended compactly.
    """
    parts: List[str] = []
    if parent_title:
        parts.append(f"РОДИТЕЛЬ [{parent_title}]")
    sym = category.symbol
    prefix = f"[{sym}] " if sym else ""
    parts.append(f"ЗАДАЧА {prefix}{task_title}")

    if completed_siblings:
        lines = "\n".join(f"\u2713 {t}" for t in completed_siblings)
        parts.append("ЧТО УЖЕ ВЫПОЛНЕНО\n" + lines)

    if research_results:
        blocks = [f"- {title}: {text.strip()}" for title, text in research_results
                  if text and text.strip()]
        if blocks:
            parts.append("РЕЗУЛЬТАТЫ ИССЛЕДОВАНИЙ\n" + "\n".join(blocks))

    return "\n\n".join(parts)

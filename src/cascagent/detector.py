"""Duplicate detection for decomposition output (CPU-side, deterministic).

LLM rule #2 says "don't produce identical subtasks" — but weak models
violate it. The DuplicateDetector filters repeats *after* parsing so the
tree never contains near-duplicate branches, and detects parent-echo and
ancestor cycles (decomposition that re-asks the same question forever).

Similarity metric: difflib.SequenceMatcher ratio on normalized text
(casefolded, punctuation-insensitive, whitespace-collapsed).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Tuple

from .models import Task

# Punctuation/symbols stripped before comparison (protocol symbols too).
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Casefold, drop punctuation, collapse whitespace."""
    s = _PUNCT_RE.sub(" ", text or "")
    s = _WS_RE.sub(" ", s).strip()
    return s.casefold()


class DuplicateDetector:
    """Fuzzy duplicate filter based on SequenceMatcher similarity."""

    def __init__(self, threshold: float = 0.75):
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self.threshold = threshold

    # ------------------------------------------------------------------ core

    def similarity(self, a: str, b: str) -> float:
        """SequenceMatcher ratio on normalized strings ([0..1])."""
        na, nb = normalize(a), normalize(b)
        if not na and not nb:
            return 1.0
        if not na or not nb:
            return 0.0
        if na == nb:
            return 1.0
        return SequenceMatcher(None, na.lower(), nb.lower()).ratio()

    def is_duplicate(self, new_task: str, existing: Iterable[str]) -> bool:
        """True if ``new_task`` is >= threshold similar to any existing one."""
        return any(self.similarity(new_task, e) >= self.threshold
                   for e in existing)

    def is_parent_repeat(self, task: str, parent: str) -> bool:
        """True if the subtask just re-states its parent (rule #1)."""
        return self.similarity(task, parent) >= self.threshold

    def detect_cycle(self, task: str, ancestors: Iterable[str]) -> bool:
        """True if ``task`` matches ANY ancestor title => recursive cycle."""
        return any(self.similarity(task, anc) >= self.threshold
                   for anc in ancestors)

    # ------------------------------------------------------------- filtering

    def filter_tasks(
        self,
        tasks: List[Task],
        parent_title: Optional[str] = None,
        ancestors: Optional[List[str]] = None,
        siblings: Optional[List[str]] = None,
    ) -> Tuple[List[Task], List[Task]]:
        """Split parsed subtasks into (kept, rejected).

        Rejection reasons (all deterministic, CPU-side):
        - exact '<atom>' tasks are always kept (they carry no title);
        - parent echo (is_parent_repeat);
        - cycle against ancestors (detect_cycle);
        - near-duplicate of an earlier kept task in the same batch;
        - near-duplicate of already-existing siblings.

        Order-preserving; never truncates legitimate output (§9.2).
        """
        kept: List[Task] = []
        rejected: List[Task] = []
        seen: List[str] = list(siblings or [])

        for task in tasks:
            if task.is_atom:
                kept.append(task)
                continue
            title = task.title
            if parent_title and self.is_parent_repeat(title, parent_title):
                rejected.append(task)
                continue
            if ancestors and self.detect_cycle(title, ancestors):
                rejected.append(task)
                continue
            if self.is_duplicate(title, seen):
                rejected.append(task)
                continue
            kept.append(task)
            seen.append(title)
        return kept, rejected

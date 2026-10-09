"""Duplicate detection for decomposition output (CPU-side, deterministic).

Protocol v2 defines TWO fundamentally different kinds of name collision
(docs/protocol.md §3.8), handled by two modes of this detector:

R1 — LOCAL dedup inside one decomposition answer (threshold 0.75):
    * a subtask that repeats the decomposed task's own brief is deleted;
    * two near-identical subtasks in the same list collapse to the first.
    If nothing survives R1 filtering, the orchestrator marks the task
    ATOMIC (that is how ``<atom>`` arises without the model saying it).

R2 — GLOBAL dedup across the whole Kanban tree (threshold ~0.90, high):
    a match with a task from ANOTHER branch never deletes work: the
    DEEPEST task is the original (the need appeared at that level); the
    shallower one becomes a link ``duplicate_of=<original id>`` whose
    status/result mirror the original.

Similarity metric: difflib.SequenceMatcher ratio on normalized text
(casefolded, punctuation-insensitive, whitespace-collapsed). Works on
``Task.brief`` (v2 field name).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Tuple

from .models import Task

# Punctuation/symbols stripped before comparison.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

#: Default thresholds (docs/protocol.md §3.8 / AGENTS.md).
LOCAL_THRESHOLD = 0.75     # R1: within one decomposition + parent echo
GLOBAL_THRESHOLD = 0.90    # R2: cross-branch identity (near-exact briefs)


def normalize(text: str) -> str:
    """Casefold, drop punctuation, collapse whitespace."""
    s = _PUNCT_RE.sub(" ", text or "")
    s = _WS_RE.sub(" ", s).strip()
    return s.casefold()


@dataclass
class DedupResult:
    """Outcome of R1 local filtering for one decomposition answer."""

    kept: List[Task]
    removed: List[Task]

    @property
    def became_atomic(self) -> bool:
        """§3.8 R1: empty list after local dedup => task is atomic."""
        return not self.kept


class DuplicateDetector:
    """Fuzzy duplicate filter based on SequenceMatcher similarity.

    ``threshold`` governs every pairwise check made through this instance;
    use two instances for the two protocol modes:
    ``DuplicateDetector()``          -> R1 local (0.75)
    ``DuplicateDetector(0.90)``      -> R2 global cross-branch
    """

    def __init__(self, threshold: float = LOCAL_THRESHOLD):
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
        """True if the subtask just re-states its parent (rule R1a)."""
        return self.similarity(task, parent) >= self.threshold

    def detect_cycle(self, task: str, ancestors: Iterable[str]) -> bool:
        """True if ``task`` matches ANY ancestor brief => recursive cycle."""
        return any(self.similarity(task, anc) >= self.threshold
                   for anc in ancestors)

    # ------------------------------------------------------- R1: local mode

    def filter_local(
        self,
        tasks: List[Task],
        parent_brief: Optional[str] = None,
        ancestors: Optional[List[str]] = None,
        siblings: Optional[List[str]] = None,
    ) -> DedupResult:
        """Rule R1 (§3.8): delete repeats inside ONE decomposition answer.

        Rejection reasons (all deterministic, CPU-side):
        - parent echo (subtask brief ≈ decomposed task brief);
        - cycle against ancestors;
        - near-duplicate of an earlier kept task in the same batch
          (first occurrence wins, order preserved);
        - near-duplicate of already-existing siblings.

        Returns :class:`DedupResult`; if ``kept`` is empty the caller marks
        the task atomic (R1 ⇒ atom). Never truncates legitimate output
        beyond explicit duplicates (§9.2 AGENTS.md).
        """
        kept: List[Task] = []
        removed: List[Task] = []
        seen: List[str] = list(siblings or [])

        for task in tasks:
            brief = task.brief
            if parent_brief and self.is_parent_repeat(brief, parent_brief):
                removed.append(task)
                continue
            if ancestors and self.detect_cycle(brief, ancestors):
                removed.append(task)
                continue
            if self.is_duplicate(brief, seen):
                removed.append(task)
                continue
            kept.append(task)
            seen.append(brief)
        return DedupResult(kept=kept, removed=removed)

    # ----------------------------------------------------- R2: global mode

    def find_global_match(
        self,
        brief: str,
        candidates: Iterable[Task],
    ) -> Optional[Task]:
        """Rule R2 (§3.8): find the best cross-branch match in the tree.

        ``candidates`` are tasks from OTHER branches (caller excludes the
        current subtree). Comparison uses this instance's threshold
        (typically GLOBAL_THRESHOLD=0.90 — only near-exact identities).

        Original selection policy (returned candidate is the ORIGINAL):
        - deepest task wins (need materialized at the lowest level);
        - equal depth → earliest created wins (first planned keeps identity;
          implemented via stable iteration order of ``candidates``).
        Returns ``None`` when nothing matches.
        """
        best: Optional[Task] = None
        best_score = 0.0
        for cand in candidates:
            score = self.similarity(brief, cand.brief)
            if score < self.threshold:
                continue
            if best is None:
                best, best_score = cand, score
                continue
            # deeper always wins; on tie keep the earlier candidate
            if cand.depth > best.depth or (
                    cand.depth == best.depth and score > best_score):
                best, best_score = cand, score
        return best

    def link_or_promote(
        self,
        new_task: Task,
        candidates: Iterable[Task],
    ) -> Tuple[Task, Optional[Task]]:
        """Apply R2 to one freshly parsed subtask.

        Returns ``(task, repointed_original)``:
        - match found, original deeper or equal → ``task.duplicate_of`` set
          to original id, no work duplicated;
        - match found but ``new_task`` is strictly deeper → the old (shallower)
          task is REPONTED: it becomes the link to ``new_task`` (which stays
          the original); returned second element is that demoted task so the
          orchestrator can persist the change;
        - no match → task unchanged, second element ``None``.
        """
        orig = self.find_global_match(new_task.brief, candidates)
        if orig is None:
            return new_task, None
        if new_task.depth > orig.depth:
            # New task is deeper → it becomes the original; old one links to it.
            orig.duplicate_of = new_task.id
            new_task.duplicate_of = None
            return new_task, orig
        # Equal or shallower than found original → new task is the link.
        new_task.duplicate_of = orig.id
        return new_task, None

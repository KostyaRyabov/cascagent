"""Core dataclasses and enums for cascagent.

Design notes (see AGENTS.md):
- ``TaskCategory`` encodes the decomposition protocol symbols:
  ``>`` RESEARCH, ``!`` MUST_DO, ``?`` DEFERRED, plus ``ATOM`` marker.
- Every LLM call is isolated; ``DecompositionCall`` records one call
  (think block + final response + timings) for the JSONL history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class TaskCategory(str, Enum):
    """Category of a subtask as produced by the decomposition protocol."""

    RESEARCH = "research"    # symbol '>'
    MUST_DO = "must_do"      # symbol '!'
    DEFERRED = "deferred"    # symbol '?'
    ATOM = "atom"            # literal '<atom>' — task is atomic
    ROOT = "root"            # the initial user query (no symbol)

    @property
    def symbol(self) -> str:
        """Protocol symbol used in LLM output lines."""
        return _CATEGORY_SYMBOL[self]

    @property
    def icon(self) -> str:
        """Pretty-print icon used by the CLI tree renderer."""
        return _CATEGORY_ICON[self]


_CATEGORY_SYMBOL: Dict[TaskCategory, str] = {
    TaskCategory.RESEARCH: ">",
    TaskCategory.MUST_DO: "!",
    TaskCategory.DEFERRED: "?",
    TaskCategory.ATOM: "<atom>",
    TaskCategory.ROOT: "",
}

_CATEGORY_ICON: Dict[TaskCategory, str] = {
    TaskCategory.RESEARCH: "\U0001F50D",   # 🔍
    TaskCategory.MUST_DO: "\u2713",        # ✓
    TaskCategory.DEFERRED: "?",            # ?
    TaskCategory.ATOM: "\u269B",           # ⚛
    TaskCategory.ROOT: "\u2514\u2500",     # └─
}

#: Symbol string -> category (for parsing LLM lines). '<atom>' handled separately.
SYMBOL_TO_CATEGORY = {
    ">": TaskCategory.RESEARCH,
    "!": TaskCategory.MUST_DO,
    "?": TaskCategory.DEFERRED,
}


def new_id() -> str:
    """Short unique id for tasks/calls."""
    return uuid.uuid4().hex[:12]


@dataclass
class Task:
    """A node in the decomposition tree. Source of truth lives in SQLite Kanban."""

    title: str
    category: TaskCategory = TaskCategory.ROOT
    depth: int = 0
    id: str = field(default_factory=new_id)
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    status: str = "pending"  # pending | done | deferred | atomic
    result: Optional[str] = None  # execution/research result text, if any

    @property
    def is_atom(self) -> bool:
        return self.category == TaskCategory.ATOM

    @property
    def is_deferred(self) -> bool:
        return self.category == TaskCategory.DEFERRED

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category.value,
            "depth": self.depth,
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            id=d.get("id") or new_id(),
            title=d["title"],
            category=TaskCategory(d.get("category", "root")),
            depth=int(d.get("depth", 0)),
            parent_id=d.get("parent_id"),
            children_ids=list(d.get("children_ids", [])),
            status=d.get("status", "pending"),
        )


@dataclass
class DecompositionCall:
    """One isolated LLM call: prompt context, raw outputs, timings."""

    task_id: str
    task_title: str
    depth: int
    think_enabled: bool
    prompt: str = ""
    think: str = ""                       # separated reasoning block
    final_response: str = ""              # content after the think block
    prefill_ms: float = 0.0               # prompt processing time
    generation_ms: float = 0.0            # token generation time
    total_ms: float = 0.0
    subtasks: List[Task] = field(default_factory=list)
    error: Optional[str] = None
    id: str = field(default_factory=new_id)

    @property
    def is_atomic(self) -> bool:
        """Atomic iff model returned exactly '<atom>' (or empty response).

        Principle: NO heuristics for atomicity — exact match only.
        """
        stripped = self.final_response.strip()
        return stripped == "" or stripped.lower() == "<atom>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "depth": self.depth,
            "think_enabled": self.think_enabled,
            "prompt": self.prompt,
            "think": self.think,
            "final_response": self.final_response,
            "prefill_ms": round(self.prefill_ms, 1),
            "generation_ms": round(self.generation_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "subtasks": [t.to_dict() for t in self.subtasks],
            "error": self.error,
        }

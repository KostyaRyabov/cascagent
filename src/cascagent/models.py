"""Core dataclasses and enums for cascagent (protocol v2).

Design notes (docs/protocol.md §4):
- The model output carries NO categories. A task is just ``brief`` +
  ``description``; ordering, dependencies and status are managed by the
  CPU-side orchestrator, not by the LLM.
- Every LLM call is isolated; ``DecompositionCall`` records one call
  (think block + final response + timings) for the JSONL history.
- Cross-branch duplicates never delete work: the shallower task becomes a
  link ``duplicate_of=<original id>`` pointing at the deepest original
  (protocol §3.8 rule R2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


def new_id() -> str:
    """Short unique id for tasks/calls."""
    return uuid.uuid4().hex[:12]


class TaskStatus(str, Enum):
    """Lifecycle state of a task (source of truth: SQLite Kanban)."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.FAILED)


@dataclass
class Task:
    """A node in the decomposition tree (protocol v2, simplified)."""

    brief: str                                   # title: RAG / dedup / tree view
    description: str = ""                        # detailed what-to-do text
    id: str = field(default_factory=new_id)
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None                 # outcome after execution
    parent_id: Optional[str] = None
    depth: int = 0
    subtasks: List[str] = field(default_factory=list)   # children ids, order = execution order
    duplicate_of: Optional[str] = None           # link to original (rule R2, §3.8)

    # ------------------------------------------------------------- helpers

    @property
    def is_link(self) -> bool:
        """True if this task mirrors another (deeper) original (§3.8 R2)."""
        return self.duplicate_of is not None

    @property
    def is_leaf_pending(self) -> bool:
        """No children yet — candidate for (lazy) decomposition."""
        return not self.subtasks

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "brief": self.brief,
            "description": self.description,
            "status": self.status.value,
            "result": self.result,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "subtasks": list(self.subtasks),
            "duplicate_of": self.duplicate_of,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            id=d.get("id") or new_id(),
            brief=d["brief"],
            description=d.get("description", ""),
            status=TaskStatus(d.get("status", "pending")),
            result=d.get("result"),
            parent_id=d.get("parent_id"),
            depth=int(d.get("depth", 0)),
            subtasks=list(d.get("subtasks", [])),
            duplicate_of=d.get("duplicate_of"),
        )


@dataclass
class DecompositionCall:
    """One isolated LLM call: prompt context, raw outputs, timings."""

    task_id: str
    task_brief: str
    depth: int
    think_enabled: bool
    prompt: str = ""
    think: str = ""                       # separated reasoning block
    final_response: str = ""              # content after the think block
    prefill_ms: float = 0.0               # prompt processing time
    generation_ms: float = 0.0            # token generation time
    total_ms: float = 0.0
    subtasks: List[Task] = field(default_factory=list)
    atomic: bool = False                  # set by orchestrator after parsing
                                          # (exact <atom>/empty OR post-R1 empty list)
    error: Optional[str] = None
    id: str = field(default_factory=new_id)

    @property
    def is_atomic(self) -> bool:
        """Atomic iff model returned exactly '<atom>' or an empty response.

        Principle (AGENTS.md §9.1): NO heuristics here — exact match only.
        Post-deduplication atomarity (R1) is decided by the orchestrator and
        recorded in the ``atomic`` flag, not derived from text here.
        """
        from .parser import is_atomic as _is_atomic  # local import: avoid cycle
        return _is_atomic(self.final_response) or self.atomic

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "task_brief": self.task_brief,
            "depth": self.depth,
            "think_enabled": self.think_enabled,
            "prompt": self.prompt,
            "think": self.think,
            "final_response": self.final_response,
            "prefill_ms": round(self.prefill_ms, 1),
            "generation_ms": round(self.generation_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "subtasks": [t.to_dict() for t in self.subtasks],
            "atomic": self.atomic,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DecompositionCall":
        return cls(
            id=d.get("id") or new_id(),
            task_id=d.get("task_id", ""),
            task_brief=d.get("task_brief", d.get("task_title", "")),
            depth=int(d.get("depth", 0)),
            think_enabled=bool(d.get("think_enabled", False)),
            prompt=d.get("prompt", ""),
            think=d.get("think", ""),
            final_response=d.get("final_response", ""),
            prefill_ms=float(d.get("prefill_ms", 0.0)),
            generation_ms=float(d.get("generation_ms", 0.0)),
            total_ms=float(d.get("total_ms", 0.0)),
            subtasks=[Task.from_dict(t) for t in d.get("subtasks", [])],
            atomic=bool(d.get("atomic", False)),
            error=d.get("error"),
        )

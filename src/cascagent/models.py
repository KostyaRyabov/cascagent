"""Core data model of cascagent — protocol v2.

Source of truth: docs/data.md §1–2 (Task/TaskStatus, snowflake id) and
docs/protocol.md §4 (protocol contract); AGENTS.md §4 summarizes both.

Decisions already taken (do not re-design):
- ``TaskCategory`` (``> ! ?``) from v1 is REMOVED (ADR-003): the model
  operates on a ``brief`` + ``description`` pair only; all "what to do
  with a task" logic lives in the CPU layer around the LLM.
- Every field is formed in ONE step inside ``__init__`` (no dataclass,
  no ``__post_init__``, no secondary id write): ``created_at`` → snowflake
  ``id``, ``embedding = embed(brief)``, ``depth`` derived from ``parent``.
  A Task instance with an incomplete set of fields is impossible by
  construction (data.md §1 п.9).
- No ``order`` field: execution order of children == their position in
  ``parent.subtasks`` (data.md §1 п.6).
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Sequence

#: Snowflake epoch of the project (config.toml [ids] epoch; part of the id
#: contract — must never be changed, data.md §2).
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_EPOCH_MS = int(EPOCH.timestamp() * 1000)


class TaskStatus(str, Enum):
    """Task lifecycle states (data.md §1; state machine — product.md §5)."""

    PENDING = "pending"        # work has NOT started yet — FIFO queue;
                               # only CPU decomposition invariants touch it
    ENRICHMENT = "enrichment"  # work started, no LLM call yet: agent pick,
                               # MCP tools, knowledge bases, context build
    RUNNING = "running"        # context ready — active LLM call
    DONE = "done"              # completed successfully
    FAILED = "failed"          # error or not enough data to proceed


class _SnowflakeIds:
    """Twitter-snowflake generator: monotonic 64-bit int with the creation
    timestamp encoded inside (bit layout — data.md §2)."""

    _MACHINE_BITS = 10
    _SEQUENCE_BITS = 12
    _MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1

    def __init__(self, node_id: int = 0):
        if not 0 <= node_id < (1 << self._MACHINE_BITS):
            raise ValueError("node_id must be in 0..1023 (data.md §2)")
        self.node_id = node_id
        self._lock = threading.Lock()
        self._last_ms = -1
        self._seq = 0

    def next_id(self, created_at: Optional[datetime] = None) -> int:
        """Next id for ``created_at`` (default: now, UTC). Monotonic: ids
        within one millisecond increase via the sequence counter.

        The epoch check runs BEFORE monotonic clamping: a pre-epoch
        timestamp must always raise ValueError — silently bumping it to
        ``_last_ms`` would mint a valid id for an invalid date and make
        validation order-dependent on generator state.
        """
        ms = (
            int(created_at.timestamp() * 1000)
            if created_at is not None
            else int(time.time() * 1000)
        )
        delta = ms - _EPOCH_MS
        if delta < 0:
            raise ValueError("created_at precedes the project epoch")
        with self._lock:
            if ms < self._last_ms:                  # clock moved backwards
                ms = self._last_ms
            if ms == self._last_ms:
                self._seq += 1
                if self._seq > self._MAX_SEQUENCE:  # seq exhausted this ms
                    while ms <= self._last_ms:
                        ms = int(time.time() * 1000)
                    self._seq = 0
            else:
                self._seq = 0
            self._last_ms = ms
            seq = self._seq
            delta = ms - _EPOCH_MS  # recompute after monotonic clamp
        return (
            (delta << (self._MACHINE_BITS + self._SEQUENCE_BITS))
            | (self.node_id << self._SEQUENCE_BITS)
            | seq
        )


#: Process-wide generator; ``node_id`` comes from config ([ids] node_id)
#: when the orchestrator is wired up (Этап 3+).
snowflake_ids = _SnowflakeIds()


def embed(text: str) -> Sequence[float]:
    """Deterministic stdlib embedding fallback for ``Task.embedding``.

    The real vector backend arrives with ``semantic.py`` (Этап 4, extras
    ``sentence-transformers``; algorithms.md §0.2 requires a clean stdlib
    fallback so the core of Этапы 1–3 installs without heavy deps). Until
    then: a stable hashed bag-of-ngrams vector. Contract: deterministic
    for the same text, fixed dimension.
    """
    dim = 64
    vec = [0.0] * dim
    norm = "".join((text or "").casefold().split())
    for n in (1, 2, 3):
        for i in range(len(norm) - n + 1):
            digest = hashlib.sha1(norm[i : i + n].encode("utf-8")).digest()
            h = int.from_bytes(digest[:4], "little")
            vec[h % dim] += 1.0 / n
    total = sum(abs(v) for v in vec) or 1.0
    return [v / total for v in vec]


class Task:
    """A node of the decomposition tree (full spec — data.md §1).

    All fields are formed in one step in ``__init__``: ``created_at`` is
    fixed, snowflake ``id`` is encoded from it right there, ``embedding``
    is computed from ``brief``, ``depth`` is incremented from ``parent``.
    """

    def __init__(
        self,
        brief: str,
        description: Optional[str] = None,
        *,
        is_atom: bool = False,
        parent: Optional["Task"] = None,
        canonical: Optional["Task"] = None,
    ):
        self.created_at = datetime.now(timezone.utc)
        self.id: int = snowflake_ids.next_id(self.created_at)
        self.brief = brief                      # one-line title (~50 tokens)
        self.description = description          # what to do (for the LLM)
        self.is_atom = is_atom                  # CPU flag: never decomposed
        self.embedding = embed(brief)           # one pass per task (A4.1)
        self.canonical = canonical              # future dedup graph (None in v1)
        self.status = TaskStatus.PENDING
        self.result: Optional[str] = None
        self.parent = parent                    # direct object link in memory
        self.depth = 0 if parent is None else parent.depth + 1
        self.subtasks: List["Task"] = []        # order == execution order
        self.started_at: Optional[datetime] = None   # set at PENDING→ENRICHMENT
        self.finished_at: Optional[datetime] = None  # set at DONE/FAILED

    def __repr__(self) -> str:
        atom = " atom" if self.is_atom else ""
        return (
            f"<Task {self.id} d={self.depth} "
            f"{self.status.value}{atom} {self.brief[:40]!r}>"
        )

"""Kanban — SQLite persistence of the task tree (docs/data.md §4).

Source of truth for the schema: data.md §4.2 (verbatim DDL below), key
operations: §4.3 (``Kanban`` class contract), conventions: WAL,
foreign_keys=ON, check_same_thread=False + external single-flight lock,
UTC ISO-8601 timestamps in TIMESTAMP columns, booleans as INTEGER 0/1,
embedding BLOB = float32 little-endian wire format (§4.2 note; the same
bytes are produced by ``embed()`` in models.py).

Experience store, not call logs (data.md §3): there is deliberately NO
history of raw LLM calls here — every request is wrapped in a Task, so
the durable artifact of a run is the *completed task*: its key text
(root → ``summary``, subtask → ``brief``) and its ``result``. Exactly
those pairs land in ``semantic_memory`` via :meth:`remember_done` at the
DONE transition and are recalled as RAG context via :meth:`recall`.

Role in the system (data.md §1): the live object tree lives in PROJECT
memory under key ``tasks``; Kanban is its persistent mirror — the source
of truth about task STATES after a crash (roadmap #3 recovery: RUNNING /
ENRICHMENT rows flip back to PENDING on resume; DONE branches are never
re-decomposed).

Order materialization (data.md §1 п.6/§2): in memory the execution order
of siblings == their position in ``parent.subtasks``; on save it is
materialized into the ``"order"`` column so it survives a reload; loads
always sort by ``("order", id)``.

``find_similar`` (data.md §4.3): SQLite has no native cosine — embeddings
are read into numpy and compared on CPU (cpu-offload P2); faiss/sqlite-vss
only past ~10k rows (open question §8, not here).

The ``llm_calls`` table (§4.2) remains in the schema for legacy DBs only;
nothing writes to it since the history redesign (data.md §3, 2026-10-13).
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from cascagent.models import Task, TaskStatus, embed

#: Exact schema from docs/data.md §4.2 (do not diverge silently).
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES tasks(id),
    "order" INTEGER NOT NULL DEFAULT 0,
    depth INTEGER NOT NULL DEFAULT 0,
    brief TEXT NOT NULL,
    description TEXT,
    is_atom INTEGER NOT NULL DEFAULT 0,
    embedding BLOB,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    summary TEXT,
    created_at TIMESTAMP NOT NULL,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_depth  ON tasks(depth);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_parent_order
    ON tasks(parent_id, "order");

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    timestamp TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    duration_ms INTEGER,
    think_enabled INTEGER,
    is_atom INTEGER,
    duplicates_removed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS research_cache (
    query_hash TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    findings TEXT NOT NULL,
    sources TEXT,
    confidence REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_brief TEXT NOT NULL,               -- RAG-ключ: summary корня / brief подзадачи
    task_result TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    source_task_id INTEGER                  -- задача-источник (1:1, data.md §7)
);
CREATE INDEX IF NOT EXISTS idx_semantic_created
    ON semantic_memory(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_source
    ON semantic_memory(source_task_id);
"""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """UTC ISO-8601 with offset (data.md §4.2 conventions); None passes."""
    if dt is None:
        return None
    if dt.tzinfo is None:                       # defensive: assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def embedding_to_blob(vec: Sequence[float]) -> bytes:
    """float32 little-endian wire format (data.md §1/§4.2)."""
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_to_embedding(blob: Optional[bytes]) -> Optional[List[float]]:
    if blob is None:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


class Kanban:
    """SQLite-backed mirror of the task tree (data.md §4.3 API)."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path),
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self._migrate()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")       # §4.2 conventions
        cur.execute("PRAGMA foreign_keys=ON")
        cur.executescript(SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        """Idempotent column additions for DBs created by older schema."""
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(tasks)").fetchall()}
        if "summary" not in cols:                    # data.md §1 (root digest)
            self.conn.execute("ALTER TABLE tasks ADD COLUMN summary TEXT")
            self.conn.commit()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    def add_task(self, task: Task, order: Optional[int] = None) -> int:
        """INSERT one node (id is the snowflake already inside Task).

        ``order`` defaults to the position in ``parent.subtasks``
        (position == index, data.md §1 п.6); roots get 0.
        Idempotent re-save: UPSERT keeps the row fresh (status/result).
        """
        if order is None:
            order = (task.parent.subtasks.index(task)
                     if task.parent is not None and task in task.parent.subtasks
                     else 0)
        # Task model (data.md §1) has no metadata field; SQL column exists
        # for future extensions without migrations (§4.2). Read defensively.
        meta = getattr(task, "metadata", None)
        self.conn.execute(
            """
            INSERT INTO tasks (id, parent_id, "order", depth, brief, description,
                               is_atom, embedding, status, result, summary,
                               created_at, started_at, finished_at, metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                parent_id=excluded.parent_id,
                "order"=excluded."order",
                depth=excluded.depth,
                brief=excluded.brief,
                description=excluded.description,
                is_atom=excluded.is_atom,
                embedding=excluded.embedding,
                status=excluded.status,
                result=excluded.result,
                summary=excluded.summary,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                metadata=excluded.metadata
            """,
            (
                task.id,
                task.parent.id if task.parent is not None else None,
                order,
                task.depth,
                task.brief,
                task.description,
                int(bool(task.is_atom)),
                embedding_to_blob(task.embedding) if task.embedding else None,
                task.status.value,
                task.result,
                getattr(task, "summary", None),     # root-only digest (§1)
                _iso(task.created_at),
                _iso(task.started_at),
                _iso(task.finished_at),
                json.dumps(meta, ensure_ascii=False) if meta else None,
            ),
        )
        self.conn.commit()
        return task.id

    def save_tree(self, root: Task) -> int:
        """Persist a whole subtree breadth-first (parents before children).

        Order is materialized from each parent's ``subtasks`` position.
        Returns the number of rows written.
        """
        count = 0
        stack: List[Task] = [root]
        while stack:
            t = stack.pop(0)
            self.add_task(t)
            count += 1
            stack = list(t.subtasks) + stack
        return count

    def update_status(self, task_id: int, status: TaskStatus,
                      result: Optional[str] = None) -> None:
        """State-machine transition write (product.md §5).

        Timestamp discipline (data.md §1): ``started_at`` set at
        PENDING→ENRICHMENT, ``finished_at`` at DONE/FAILED.
        """
        now = _iso(datetime.now(timezone.utc))
        sets = ["status=?"]
        args: List[Any] = [status.value if isinstance(status, TaskStatus) else status]
        if status == TaskStatus.ENRICHMENT:
            sets.append("started_at=?")
            args.append(now)
        if status in (TaskStatus.DONE, TaskStatus.FAILED):
            sets.append("finished_at=?")
            args.append(now)
        if result is not None:
            sets.append("result=?")
            args.append(result)
        args.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", args)
        self.conn.commit()

    def get_task(self, task_id: int) -> Optional[Task]:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?",
                                (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def get_pending_tasks(self, depth: Optional[int] = None) -> List[Task]:
        q = "SELECT * FROM tasks WHERE status='pending'"
        args: List[Any] = []
        if depth is not None:
            q += " AND depth=?"
            args.append(depth)
        q += ' ORDER BY id ASC'                     # snowflake monotonic = FIFO
        return [self._row_to_task(r)
                for r in self.conn.execute(q, args).fetchall()]

    def get_siblings(self, task_id: int) -> List[Task]:
        """Same parent, ordered by ("order", id) — data.md §4.3."""
        row = self.conn.execute(
            'SELECT parent_id FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            return []
        parent_id = row["parent_id"]
        q = 'SELECT * FROM tasks WHERE "order" IS NOT NULL'
        args: List[Any] = []
        if parent_id is None:
            q += " AND parent_id IS NULL"
        else:
            q += " AND parent_id=?"
            args.append(parent_id)
        q += ' ORDER BY "order", id'
        rows = self.conn.execute(q, args).fetchall()
        return [self._row_to_task(r) for r in rows if r["id"] != task_id]

    def done_siblings(self, task_id: int) -> List[Task]:
        """Completed siblings for enrichment (data.md §7: only DONE)."""
        return [t for t in self.get_siblings(task_id)
                if t.status == TaskStatus.DONE]

    def get_subtasks(self, task_id: int) -> List[Task]:
        rows = self.conn.execute(
            'SELECT * FROM tasks WHERE parent_id=? ORDER BY "order", id',
            (task_id,)).fetchall()
        return [self._row_to_task(r) for r in rows]

    def load_tree(self) -> List[Task]:
        """Rebuild live objects: roots (parent_id IS NULL) with links.

        Invariants checked on load (data.md §2): every non-root has an
        existing parent_id; children sorted by the materialized "order".
        Returns the list of root Tasks (subtasks/parent wired in memory).
        """
        rows = self.conn.execute(
            "SELECT * FROM tasks ORDER BY parent_id, \"order\", id").fetchall()
        by_id: Dict[int, Task] = {}
        for r in rows:
            t = self._row_to_task(r)
            by_id[t.id] = t
        roots: List[Task] = []
        for t in by_id.values():
            pid = self.conn.execute(
                "SELECT parent_id FROM tasks WHERE id=?", (t.id,)
            ).fetchone()["parent_id"]
            if pid is None:
                roots.append(t)
            else:
                parent = by_id.get(pid)
                if parent is None:               # broken link — skip wiring
                    continue
                t.parent = parent
                parent.subtasks.append(t)
        return roots

    def recover_interrupted(self) -> int:
        """Resume hygiene (roadmap.md §3.3 п.1): RUNNING/ENRICHMENT → PENDING.

        Those tasks could not have completed silently; side-effects are
        idempotentized by the executor protocol. Returns flipped count.
        """
        cur = self.conn.execute(
            "UPDATE tasks SET status='pending', started_at=NULL "
            "WHERE status IN ('running','enrichment')")
        self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Similarity search (data.md §4.3: numpy cosine on CPU, P2)
    # ------------------------------------------------------------------
    def find_similar(self, embedding: bytes, top_k: int = 3,
                     exclude_id: Optional[int] = None) -> List[Task]:
        """Cosine top-K over stored embeddings (small-N scan; faiss lands
        past ~10k rows — data.md §8 open question, not v0.x)."""
        import numpy as np
        q = np.frombuffer(embedding, dtype="<f4")
        if q.size == 0:
            return []
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE embedding IS NOT NULL"
            + (" AND id != ?" if exclude_id is not None else ""),
            ((exclude_id,) if exclude_id is not None else ())).fetchall()
        scored: List[tuple] = []
        for r in rows:
            v = np.frombuffer(r["embedding"], dtype="<f4")
            if v.size != q.size:
                continue
            denom = float(np.linalg.norm(v) * np.linalg.norm(q)) or 1.0
            scored.append((float(np.dot(v, q)) / denom, r))
        scored.sort(key=lambda x: -x[0])
        return [self._row_to_task(r) for _, r in scored[:top_k]]

    # ------------------------------------------------------------------
    # Experience store (data.md §3: completed tasks, NOT raw LLM calls)
    # ------------------------------------------------------------------
    def remember_done(self, task_id: int) -> Optional[int]:
        """Mirror a DONE task into ``semantic_memory`` (idempotent).

        The RAG key is the task's own text: ``summary`` for the root task
        (digest of the user request — brief ~50 tokens is too coarse for
        a whole request), ``brief`` for subtasks; the value is
        ``result``. Only correctly completed tasks qualify: status must
        be DONE and result non-empty (data.md §3, 2026-10-13 redesign).
        Re-call after an updated result refreshes the row (task ↔ memory
        is 1:1, data.md §7). Returns the memory row id, or None when the
        task does not qualify / is absent.
        """
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["status"] != TaskStatus.DONE.value:
            return None
        result = row["result"]
        if not result or not result.strip():
            return None
        key_text = row["summary"] or row["brief"]
        existing = self.conn.execute(
            "SELECT id FROM semantic_memory WHERE source_task_id=?",
            (task_id,)).fetchone()
        blob = embedding_to_blob(list(embed(key_text)))
        now = _iso(datetime.now(timezone.utc))
        if existing is not None:
            self.conn.execute(
                """UPDATE semantic_memory SET task_brief=?, task_result=?,
                       embedding=?, created_at=? WHERE id=?""",
                (key_text, result, blob, now, existing["id"]))
            self.conn.commit()
            return int(existing["id"])
        cur = self.conn.execute(
            "INSERT INTO semantic_memory (task_brief, task_result,"
            " embedding, created_at, source_task_id) VALUES (?,?,?,?,?)",
            (key_text, result, blob, now, task_id))
        self.conn.commit()
        return int(cur.lastrowid)

    def recall(self, query: str, top_k: int = 3,
               min_similarity: float = 0.0) -> List[Dict[str, Any]]:
        """RAG over completed-task experience (algorithms.md §3 contract).

        Encodes ``query`` with the same backend as task embeddings and
        returns [{"task_brief", "task_result", "similarity"}] desc by
        similarity — the raw material for
        ``EnrichedContext.semantic_memories`` (data.md §6). Rows whose
        source task was deleted from the kanban are dropped here (GC).
        """
        rows = self.recall_semantic(embedding_to_blob(list(embed(query))),
                                    top_k=top_k * 2)
        out: List[Dict[str, Any]] = []
        for r, sim in rows:
            if sim < min_similarity:
                continue
            if r["source_task_id"] is not None:
                alive = self.conn.execute(
                    "SELECT 1 FROM tasks WHERE id=?",
                    (r["source_task_id"],)).fetchone()
                if alive is None:
                    continue
            out.append({"task_brief": r["task_brief"],
                        "task_result": r["task_result"],
                        "similarity": sim})
            if len(out) >= top_k:
                break
        return out

    def forget_old(self, days: int = 30, max_keep: int = 1000) -> int:
        """GC against unbounded growth (algorithms.md §3): first by age,
        then trim-by-novelty down to ``max_keep``. Returns removed rows."""
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
        cur = self.conn.execute(
            "DELETE FROM semantic_memory WHERE created_at < ?", (cutoff,))
        removed = cur.rowcount
        keep_ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM semantic_memory ORDER BY created_at DESC"
            " LIMIT ?", (max_keep,)).fetchall()]
        if keep_ids:
            placeholders = ",".join("?" * len(keep_ids))
            removed += self.conn.execute(
                f"DELETE FROM semantic_memory WHERE id NOT IN"
                f" ({placeholders})", keep_ids).rowcount
        else:
            removed += self.conn.execute(
                "SELECT COUNT(*) FROM semantic_memory").fetchone()[0]
            self.conn.execute("DELETE FROM semantic_memory")
        self.conn.commit()
        return removed

    # ------------------------------------------------------------------
    # Semantic memory rows (vector scan used by :meth:`recall`)
    # ------------------------------------------------------------------
    def remember_semantic(self, task_brief: str, task_result: str,
                          embedding: Optional[Sequence[float]] = None
                          ) -> int:
        """Low-level INSERT into semantic_memory.

        Prefer :meth:`remember_done` (task-centric, idempotent); this
        helper exists for tests and manual seeding without a task row.
        """
        if embedding is None:
            embedding = list(embed(task_brief))
        cur = self.conn.execute(
            "INSERT INTO semantic_memory (task_brief, task_result,"
            " embedding, created_at) VALUES (?,?,?,?)",
            (task_brief, task_result,
             embedding_to_blob(embedding), _iso(datetime.now(timezone.utc))),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recall_semantic(self, embedding: bytes,
                        top_k: int = 3) -> List[tuple]:
        """Cosine top-K over semantic_memory (numpy on CPU, like
        find_similar; data.md §8 — faiss only past ~10k rows).

        Returns [(sqlite3.Row, similarity)] sorted by similarity desc.
        """
        import numpy as np
        q = np.frombuffer(embedding, dtype="<f4")
        if q.size == 0:
            return []
        scored: List[tuple] = []
        for r in self.conn.execute(
                "SELECT * FROM semantic_memory").fetchall():
            v = np.frombuffer(r["embedding"], dtype="<f4")
            if v.size != q.size:
                continue
            denom = float(np.linalg.norm(v) * np.linalg.norm(q)) or 1.0
            scored.append((r, float(np.dot(v, q)) / denom))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    # ------------------------------------------------------------------
    # Backup (data.md §4.4)
    # ------------------------------------------------------------------
    def backup(self, backups_dir: Path | str = "./data/backups",
               keep: int = 5) -> Path:
        """sqlite3 online backup + rotation (last N copies kept)."""
        d = Path(backups_dir)
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = d / f"kanban-{ts}.db"
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                self.conn.backup(dst)
        finally:
            dst.close()
        copies = sorted(d.glob("kanban-*.db"))
        for old in copies[:-keep] if keep > 0 else []:
            old.unlink()
        return dest

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        """DB row → detached Task object (links wired by load_tree).

        The snowflake id/created_at pair is restored verbatim — Task's
        constructor would mint a NEW id, so the fields are overwritten
        right after construction (documented deviation: loading is not
        creating; the generator contract applies to new nodes only).
        """
        t = Task.__new__(Task)
        t.id = row["id"]
        t.created_at = _parse_iso(row["created_at"]) or datetime.now(timezone.utc)
        t.brief = row["brief"]
        t.description = row["description"]
        t.is_atom = bool(row["is_atom"])
        t.embedding = blob_to_embedding(row["embedding"]) or []
        t.canonical = None                       # v1: dedup graph unused
        t.status = TaskStatus(row["status"])
        t.result = row["result"]
        t.parent = None
        t.depth = row["depth"]
        t.subtasks = []
        # summary column absent in pre-migration legacy rows — defensive.
        t.summary = row["summary"] if "summary" in row.keys() else None
        t.started_at = _parse_iso(row["started_at"])
        t.finished_at = _parse_iso(row["finished_at"])
        return t

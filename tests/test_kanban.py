"""Unit tests for the Kanban SQLite persistence (cascagent.kanban).

No LLM, no network (§ AGENTS.md): everything runs against a temp DB via
the ``tmp_path`` fixture. Covers data.md §4.2/§4.3 contracts: schema and
PRAGMAs, add_task UPSERT idempotency, order materialization, status
transitions with timestamp discipline, load_tree parent/child wiring,
recover_interrupted (roadmap #3), find_similar cosine scan, experience
store (remember_done/recall — key = summary|root else brief), GC
(forget_old), legacy semantic helpers and backup rotation.
"""

from __future__ import annotations

import sqlite3
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from cascagent.kanban import (
    Kanban, blob_to_embedding, embedding_to_blob,
)
from cascagent.models import Task, TaskStatus, embed


@pytest.fixture()
def kb(tmp_path):
    k = Kanban(tmp_path / "kanban.db")
    yield k
    k.close()


def make_tree() -> Task:
    """root ─ A ─ B(atom); C is second child of root.

    ``Task(parent=...)`` only sets the backlink (data.md §1: children are
    appended by the decomposer), so subtasks are wired explicitly here.
    """
    root = Task("Собрать сад", description="озеленение участка",
                summary="благоустройство сада")
    a = Task("Выбрать растения", parent=root)
    b = Task("Купить семена", parent=a, is_atom=True)
    c = Task("Постить грядки", parent=root)
    root.subtasks += [a, c]
    a.subtasks.append(b)
    return root


# ---------------------------------------------------------------------------
# Helpers: blobs & timestamps
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_blob_roundtrip_float32_le(self):
        vec = [1.0, -2.5, 0.125]
        blob = embedding_to_blob(vec)
        assert len(blob) == 4 * len(vec)
        assert blob_to_embedding(blob) == pytest.approx(vec)

    def test_blob_none_passthrough(self):
        assert blob_to_embedding(None) is None

    def test_embedding_dim_matches_embed(self):
        blob = embedding_to_blob(list(embed("что-то")))
        assert len(blob) == 4 * len(embed("что-то"))


# ---------------------------------------------------------------------------
# Schema & connection conventions (data.md §4.2)
# ---------------------------------------------------------------------------

class TestSchema:
    def test_tables_and_pragmas(self, kb):
        names = {r[0] for r in kb.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"tasks", "semantic_memory", "llm_calls"} <= names
        assert kb.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert kb.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_init_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "a" / "b" / "k.db"
        k = Kanban(nested)
        assert nested.exists()
        k.close()

    def test_reopen_is_idempotent(self, tmp_path):
        db = tmp_path / "k.db"
        k1 = Kanban(db)
        t = Task("X")
        k1.add_task(t)
        k1.close()
        k2 = Kanban(db)                      # executescript IF NOT EXISTS
        assert k2.get_task(t.id) is not None
        k2.close()


# ---------------------------------------------------------------------------
# add_task / save_tree — order materialization (§1 п.6)
# ---------------------------------------------------------------------------

class TestAddTask:
    def test_fields_roundtrip(self, kb):
        t = Task("Кратко", "Подробно", is_atom=True)
        t.status = TaskStatus.DONE
        t.result = "готово"
        tid = kb.add_task(t)
        assert tid == t.id                   # snowflake id kept, not re-minted
        got = kb.get_task(tid)
        assert got.brief == "Кратко"
        assert got.description == "Подробно"
        assert got.is_atom is True
        assert got.status is TaskStatus.DONE
        assert got.result == "готово"
        assert got.embedding == pytest.approx(list(embed("Кратко")), abs=1e-6)
        assert got.created_at == t.created_at

    def test_order_from_parent_position(self, kb):
        root = make_tree()
        kb.save_tree(root)
        rows = kb.conn.execute(
            'SELECT brief, "order" FROM tasks WHERE parent_id=? ORDER BY "order"',
            (root.id,)).fetchall()
        assert [(r["brief"], r["order"]) for r in rows] == [
            ("Выбрать растения", 0), ("Постить грядки", 1)]
        assert kb.conn.execute(
            'SELECT "order" FROM tasks WHERE id=?', (root.id,)
        ).fetchone()[0] == 0                 # roots get 0

    def test_upsert_idempotent_update(self, kb):
        t = Task("Один раз")
        kb.add_task(t)
        t.result = "после апдейта"
        t.status = TaskStatus.DONE
        kb.add_task(t)                       # same id → DO UPDATE
        n = kb.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert n == 1
        got = kb.get_task(t.id)
        assert got.result == "после апдейта"
        assert got.status is TaskStatus.DONE

    def test_save_tree_counts_breadth_first(self, kb):
        root = make_tree()
        assert kb.save_tree(root) == 4
        assert len(kb.get_subtasks(root.id)) == 2
        assert len(kb.get_subtasks(root.subtasks[0].id)) == 1


# ---------------------------------------------------------------------------
# update_status — state machine timestamps (product.md §5)
# ---------------------------------------------------------------------------

class TestUpdateStatus:
    def test_enrichment_sets_started_at(self, kb):
        t = Task("S")
        kb.add_task(t)
        kb.update_status(t.id, TaskStatus.ENRICHMENT)
        got = kb.get_task(t.id)
        assert got.status is TaskStatus.ENRICHMENT
        assert got.started_at is not None
        assert got.finished_at is None

    def test_done_sets_finished_at_and_result(self, kb):
        t = Task("S")
        kb.add_task(t)
        kb.update_status(t.id, TaskStatus.DONE, result="ок")
        got = kb.get_task(t.id)
        assert got.status is TaskStatus.DONE
        assert got.result == "ок"
        assert got.finished_at is not None

    def test_failed_also_stamps_finished(self, kb):
        t = Task("S")
        kb.add_task(t)
        kb.update_status(t.id, TaskStatus.FAILED)
        assert kb.get_task(t.id).finished_at is not None

    def test_running_touches_only_status(self, kb):
        t = Task("S")
        kb.add_task(t)
        kb.update_status(t.id, TaskStatus.RUNNING)
        got = kb.get_task(t.id)
        assert got.status is TaskStatus.RUNNING
        assert got.started_at is None and got.finished_at is None


# ---------------------------------------------------------------------------
# Queries: pending FIFO, siblings, subtasks ordering
# ---------------------------------------------------------------------------

class TestQueries:
    def test_get_pending_tasks_fifo_by_id(self, kb):
        t1 = Task("first")
        t2 = Task("second")
        done = Task("done")
        kb.add_task(t1); kb.add_task(t2)
        kb.add_task(done)
        kb.update_status(done.id, TaskStatus.DONE)
        pend = kb.get_pending_tasks()
        assert [t.brief for t in pend] == ["first", "second"]  # snowflake asc

    def test_pending_filtered_by_depth(self, kb):
        root = make_tree()
        kb.save_tree(root)
        kb.update_status(root.id, TaskStatus.DONE)
        d1 = kb.get_pending_tasks(depth=1)
        assert {t.brief for t in d1} == {"Выбрать растения", "Постить грядки"}

    def test_siblings_ordered_and_excluding_self(self, kb):
        root = make_tree()
        kb.save_tree(root)
        a, c = root.subtasks
        sibs = kb.get_siblings(a.id)
        assert [s.id for s in sibs] == [c.id]
        assert kb.get_siblings(root.id) == []   # root has no siblings

    def test_done_siblings_only_done(self, kb):
        root = make_tree()
        kb.save_tree(root)
        a, c = root.subtasks
        kb.update_status(c.id, TaskStatus.DONE, result="g")
        done = kb.done_siblings(a.id)         # detached rows, not the same objects
        assert [t.id for t in done] == [c.id]
        assert kb.done_siblings(c.id) == []

    def test_get_task_missing_returns_none(self, kb):
        assert kb.get_task(123456789) is None


# ---------------------------------------------------------------------------
# load_tree — live object rebuild (§2)
# ---------------------------------------------------------------------------

class TestLoadTree:
    def test_wires_parents_children_order(self, kb):
        root = make_tree()
        kb.save_tree(root)
        roots = kb.load_tree()
        assert len(roots) == 1
        r = roots[0]
        assert r.id == root.id
        assert [t.brief for t in r.subtasks] == ["Выбрать растения",
                                                 "Постить грядки"]
        a = r.subtasks[0]
        assert a.parent is r
        assert a.depth == 1
        assert a.subtasks[0].brief == "Купить семена"
        assert a.subtasks[0].is_atom is True
        assert a.subtasks[0].depth == 2

    def test_summary_survives_reload(self, kb):
        root = make_tree()
        kb.save_tree(root)
        assert kb.load_tree()[0].summary == "благоустройство сада"

    def test_orphan_row_skipped(self, kb):
        t = Task("child")
        kb.conn.execute("PRAGMA foreign_keys=OFF")   # simulate broken legacy DB
        kb.conn.execute(
            "INSERT INTO tasks (id, parent_id, \"order\", depth, brief,"
            " status, created_at) VALUES (?,?,?,?,?,?,?)",
            (t.id, 999999999, 0, 1, "child", "pending",
             "2026-10-13T00:00:00+00:00"))
        kb.conn.commit()
        kb.conn.execute("PRAGMA foreign_keys=ON")
        assert kb.load_tree() == []          # broken link → not wired/crash


# ---------------------------------------------------------------------------
# recover_interrupted (roadmap #3)
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_flips_running_and_enrichment_to_pending(self, kb):
        t1, t2, t3 = Task("a"), Task("b"), Task("c")
        for t in (t1, t2, t3):
            kb.add_task(t)
        kb.update_status(t1.id, TaskStatus.ENRICHMENT)
        kb.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (t2.id,))
        kb.conn.commit()
        kb.update_status(t3.id, TaskStatus.DONE)
        assert kb.recover_interrupted() == 2
        assert kb.get_task(t1.id).status is TaskStatus.PENDING
        assert kb.get_task(t1.id).started_at is None
        assert kb.get_task(t3.id).status is TaskStatus.DONE

    def test_noop_on_clean_db(self, kb):
        assert kb.recover_interrupted() == 0


# ---------------------------------------------------------------------------
# find_similar — numpy cosine on CPU (§4.3, P2)
# ---------------------------------------------------------------------------

class TestFindSimilar:
    def test_exact_match_ranks_first_and_excluded(self, kb):
        t1 = Task("полить газон")
        t2 = Task("починить забор")
        kb.add_task(t1); kb.add_task(t2)
        hits = kb.find_similar(embedding_to_blob(list(embed("полить газон"))),
                               top_k=1, exclude_id=t1.id)
        assert [h.id for h in hits] == [t2.id]     # only candidate left

    def test_top_k_limits(self, kb):
        for i in range(5):
            kb.add_task(Task(f"задача {i}"))
        q = embedding_to_blob(list(embed("задача 3")))
        assert len(kb.find_similar(q, top_k=2)) == 2

    def test_empty_embedding_returns_empty(self, kb):
        kb.add_task(Task("x"))
        assert kb.find_similar(b"") == []

    def test_dimension_mismatch_rows_skipped(self, kb):
        t = Task("y")
        kb.conn.execute("INSERT INTO tasks (id, brief, status, embedding,"
                        " created_at) VALUES (?,?,?,?,?)",
                        (t.id, "y", "pending", struct.pack("<3f", 1, 2, 3),
                         "2026-10-13T00:00:00+00:00"))
        kb.conn.commit()
        q = embedding_to_blob([0.1] * 64)
        assert kb.find_similar(q) == []            # dim 3 vs 64 → skipped


# ---------------------------------------------------------------------------
# Experience store: remember_done / recall (data.md §3 redesign)
# ---------------------------------------------------------------------------

class TestExperienceStore:
    def _done_root(self, kb) -> Task:
        root = Task("Собрать сад", summary="озеленение")
        root.status = TaskStatus.DONE
        root.result = "сад собран"
        kb.add_task(root)
        return root

    def test_remember_done_uses_summary_for_root(self, kb):
        root = self._done_root(kb)
        mid = kb.remember_done(root.id)
        assert mid is not None
        row = kb.conn.execute("SELECT * FROM semantic_memory WHERE id=?",
                              (mid,)).fetchone()
        assert row["task_brief"] == "озеленение"    # summary wins for root
        assert row["task_result"] == "сад собран"
        assert row["source_task_id"] == root.id

    def test_remember_done_uses_brief_for_subtask(self, kb):
        root = self._done_root(kb)
        sub = Task("Выбрать растения", parent=root)
        sub.status = TaskStatus.DONE
        sub.result = "яблоня и груша"
        kb.add_task(sub)
        mid = kb.remember_done(sub.id)
        row = kb.conn.execute("SELECT task_brief FROM semantic_memory"
                              " WHERE id=?", (mid,)).fetchone()
        assert row["task_brief"] == "Выбрать растения"

    def test_remember_done_refuses_non_done_or_empty(self, kb):
        t = Task("pending one")
        kb.add_task(t)
        assert kb.remember_done(t.id) is None       # not DONE
        t.status = TaskStatus.DONE                  # DONE but empty result
        kb.add_task(t)
        assert kb.remember_done(t.id) is None
        assert kb.remember_done(999999) is None     # absent task

    def test_remember_done_idempotent_refresh(self, kb):
        root = self._done_root(kb)
        m1 = kb.remember_done(root.id)
        root.result = "сад собран v2"
        kb.add_task(root)
        m2 = kb.remember_done(root.id)
        assert m1 == m2                             # 1:1 task↔memory (§7)
        assert kb.conn.execute("SELECT COUNT(*) FROM semantic_memory"
                               ).fetchone()[0] == 1
        row = kb.conn.execute("SELECT task_result FROM semantic_memory"
                              " WHERE id=?", (m2,)).fetchone()
        assert row["task_result"] == "сад собран v2"

    def test_recall_returns_sorted_experience(self, kb):
        root = self._done_root(kb)
        kb.remember_done(root.id)
        hits = kb.recall("озеленение", top_k=3)
        assert hits
        assert hits[0]["task_result"] == "сад собран"
        assert set(hits[0]) == {"task_brief", "task_result", "similarity"}
        sims = [h["similarity"] for h in hits]
        assert sims == sorted(sims, reverse=True)

    def test_recall_min_similarity_filter(self, kb):
        root = self._done_root(kb)
        kb.remember_done(root.id)
        assert kb.recall("совершенно unrelated текст про космос",
                         min_similarity=0.999) == []

    def test_recall_gcs_deleted_source_task(self, kb):
        root = self._done_root(kb)
        kb.remember_done(root.id)
        assert kb.recall("озеленение")              # present
        kb.conn.execute("DELETE FROM tasks WHERE id=?", (root.id,))
        kb.conn.commit()
        assert kb.recall("озеленение") == []        # source gone → dropped


# ---------------------------------------------------------------------------
# forget_old — GC (algorithms.md §3)
# ---------------------------------------------------------------------------

class TestForgetOld:
    def _seed(self, kb, n: int, age_days: int = 0) -> None:
        for i in range(n):
            kb.remember_semantic(f"brief {i}", f"result {i}")
        if age_days:
            cutoff_ts = datetime.now(timezone.utc) - timedelta(days=age_days)
            kb.conn.execute(
                "UPDATE semantic_memory SET created_at=?",
                (cutoff_ts.isoformat(),))
            kb.conn.commit()

    def test_age_cutoff_removes_old(self, kb):
        self._seed(kb, 3, age_days=45)
        assert kb.forget_old(days=30, max_keep=1000) == 3
        assert kb.conn.execute("SELECT COUNT(*) FROM semantic_memory"
                               ).fetchone()[0] == 0

    def test_trim_by_max_keep_keeps_newest(self, kb):
        self._seed(kb, 5)
        removed = kb.forget_old(days=3650, max_keep=2)
        assert removed == 3
        assert kb.conn.execute("SELECT COUNT(*) FROM semantic_memory"
                               ).fetchone()[0] == 2

    def test_fresh_rows_untouched(self, kb):
        self._seed(kb, 2)
        assert kb.forget_old(days=30, max_keep=1000) == 0


# ---------------------------------------------------------------------------
# Legacy low-level semantic helpers
# ---------------------------------------------------------------------------

class TestSemanticHelpers:
    def test_remember_and_recall_semantic(self, kb):
        kb.remember_semantic("полить газон", " polito")
        kb.remember_semantic("починить забор", "fix")
        q = embedding_to_blob(list(embed("полить газон")))
        rows = kb.recall_semantic(q, top_k=1)
        assert len(rows) == 1
        r, sim = rows[0]
        assert r["task_brief"] == "полить газон"
        assert sim == pytest.approx(1.0, abs=1e-5)

    def test_recall_semantic_empty_query(self, kb):
        kb.remember_semantic("a", "b")
        assert kb.recall_semantic(b"") == []


# ---------------------------------------------------------------------------
# Backup & rotation (data.md §4.4)
# ---------------------------------------------------------------------------

class TestBackup:
    def test_backup_copy_is_readable(self, kb, tmp_path):
        t = Task("backup me")
        kb.add_task(t)
        dest = kb.backup(tmp_path / "backups", keep=5)
        assert dest.exists()
        con = sqlite3.connect(dest)
        try:
            assert con.execute("SELECT brief FROM tasks WHERE id=?",
                               (t.id,)).fetchone()[0] == "backup me"
        finally:
            con.close()

    def test_rotation_keeps_last_n(self, kb, tmp_path):
        d = tmp_path / "backups"
        for _ in range(4):
            kb.backup(d, keep=2)
        assert len(list(d.glob("kanban-*.db"))) <= 2

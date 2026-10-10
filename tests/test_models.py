"""Unit tests for cascagent.models — Task/TaskStatus/snowflake/embed (v2)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cascagent.models import EPOCH, Task, TaskStatus, embed, snowflake_ids


class TestTaskStatus:
    def test_values(self):
        assert [s.value for s in TaskStatus] == [
            "pending", "enrichment", "running", "done", "failed"]

    def test_str_enum(self):
        assert TaskStatus.PENDING == "pending"


class TestTaskInit:
    def test_all_fields_one_step(self):
        t = Task("Создать модель User", "Описать поля id и email")
        assert t.brief == "Создать модель User"
        assert t.description == "Описать поля id и email"
        assert t.is_atom is False
        assert t.status is TaskStatus.PENDING
        assert t.result is None
        assert t.parent is None
        assert t.depth == 0
        assert t.subtasks == []
        assert t.canonical is None
        assert t.started_at is None and t.finished_at is None
        assert isinstance(t.id, int) and t.id > 0
        assert isinstance(t.created_at, datetime)
        assert t.created_at.tzinfo is not None

    def test_child_depth_from_parent(self):
        root = Task("Создать REST API")
        child = Task("Настроить FastAPI", parent=root)
        grand = Task("Создать app.py", parent=child)
        assert (root.depth, child.depth, grand.depth) == (0, 1, 2)
        assert child.parent is root

    def test_atom_flag_and_description_independent(self):
        a = Task("Проверить CORS", is_atom=True)
        assert a.is_atom and a.description is None

    def test_embedding_formed_in_init(self):
        t = Task("Написать тесты")
        assert list(t.embedding) == list(embed(t.brief))

    def test_ids_unique_monotonic(self):
        ids = [Task(f"Задача {i}").id for i in range(500)]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    def test_id_encodes_created_at(self):
        t = Task("Хронометраж")
        ts_ms = t.created_at.timestamp() * 1000
        # timestamp occupies the high bits above machine+sequence (data.md §2)
        decoded_ms = (t.id >> 22) + EPOCH.timestamp() * 1000
        assert abs(decoded_ms - ts_ms) < 2.0


class TestSnowflakeEdgeCases:
    def test_before_epoch_raises(self):
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        try:
            snowflake_ids.next_id(old)
        except ValueError:
            return
        raise AssertionError("expected ValueError for pre-epoch timestamp")

    def test_invalid_node_id(self):
        from cascagent.models import _SnowflakeIds
        for bad in (-1, 1024):
            try:
                _SnowflakeIds(node_id=bad)
            except ValueError:
                continue
            raise AssertionError("expected ValueError for node_id out of range")


class TestEmbed:
    def test_deterministic(self):
        assert list(embed("Создать модель User")) == list(
            embed("Создать модель User"))

    def test_different_texts_differ(self):
        assert list(embed("abc")) != list(embed("xyz"))

    def test_fixed_dimension_and_normalized(self):
        v = embed("Что угодно")
        assert len(v) == 64
        assert abs(sum(abs(x) for x in v) - 1.0) < 1e-9

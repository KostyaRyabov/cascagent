"""Unit tests for cascagent.plugins.task_parser — TaskParserPlugin as PostPlugin.

Covers the three-stage pipeline of process.md §1.3 through the plugin API:
parse → filter (F1–F3 + pairwise dedup, logic absorbed from the removed
DuplicateDetector) → attach subtree to parent in the `tasks` key of the
PROJECT memory (tasks are a project entity); plus the §4.4 result contract
and idempotency (§12.2 п.4). sanitize/normalize are GONE (leader order):
comparisons run on raw strings.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "src"))

from cascagent.models import Task
from cascagent.memory import Memory
from cascagent.plugins.task_parser import TASKS_KEY, TaskParserPlugin

PARENT_BRIEF = "Написать блог-приложение на FastAPI"


def make_parent(brief: str = PARENT_BRIEF) -> Task:
    return Task(brief=brief)


def run_plugin(response: str, task: Task, memory=None):
    """Bind a fresh Memory as the plugin's PROJECT store, then one pass."""
    memory = memory if memory is not None else Memory("test.project")
    tp = TaskParserPlugin().bind(project_memory=memory,
                                 local_memory=Memory("test.local"))
    result = tp.on_output(response, {"task": task})
    return result, memory


class TestStage1Parse:
    def test_two_subtasks_become_tasks(self):
        resp = ("Создать модель поста\nОписать поля title и body\n\n"
                "Написать роутер постов\nCRUD эндпоинты в main.py")
        parent = make_parent()
        seen = []
        mem = Memory("test.project")
        mem.subscribe("tasks", lambda k, v: seen.append((k, len(v))))
        tp = TaskParserPlugin().bind(project_memory=mem,
                                     local_memory=Memory("test.local"))
        result = tp.on_output(resp, {"task": parent})
        assert result["action"] == "accept"
        assert result["metadata"]["children"] == 2
        assert all(isinstance(t, Task) for t in result["parsed"])
        assert result["parsed"][0].depth == parent.depth + 1
        assert result["parsed"][0].parent is parent
        # Glob subscription fires on the tree write (introspection instead
        # of the removed all()).
        assert seen == [("tasks", 3)]   # parent + 2 children

    def test_global_atom_no_children(self):
        parent = make_parent()
        result, _ = run_plugin("<atom>", parent)
        assert result["parsed"] == []
        assert result["metadata"]["is_atomic"] is True
        assert parent.is_atom is True
        assert parent.subtasks == []

    def test_empty_response_is_atom(self):
        parent = make_parent()
        result, _ = run_plugin("", parent)
        assert result["metadata"]["is_atomic"] is True

    def test_marker_anywhere_is_global_atom(self):
        """Plain ``ATOM_MARKER in response`` (decision 2026-10-11): any
        occurrence of <atom> marks the WHOLE answer atomic — no children."""
        resp = "Открыть файл settings.py\n<atom>"
        parent = make_parent()
        result, _ = run_plugin(resp, parent)
        assert result["parsed"] == []
        assert result["metadata"]["is_atomic"] is True
        assert parent.is_atom is True

    def test_is_atomic_plain_marker_check(self):
        """Decision 2026-10-11: is_atomic == plain ATOM_MARKER-in-response."""
        assert TaskParserPlugin.is_atomic("<atom>") is True
        assert TaskParserPlugin.is_atomic("что-то\n<atom>\nещё") is True
        assert TaskParserPlugin.is_atomic("Обычный ответ") is False
        assert TaskParserPlugin.is_atomic("") is False


class TestStage2Filtering:
    def test_f2_parent_echo_removed(self):
        """Ребёнок ≈ родитель (protocol §3.7) — логика бывшего detector."""
        resp = (f"{PARENT_BRIEF}\nПолностью переделать всё приложение\n\n"
                "Создать модель поста\nПоля title и body")
        parent = make_parent()
        result, _ = run_plugin(resp, parent)
        assert result["metadata"]["duplicates_removed"] == 1
        assert len(result["parsed"]) == 1
        assert result["parsed"][0].brief == "Создать модель поста"

    def test_f3_all_children_are_echoes_parent_becomes_atom(self):
        resp = f"{PARENT_BRIEF}\nЗаново написать блог-приложение на FastAPI"
        parent = make_parent()
        result, _ = run_plugin(resp, parent)
        assert result["parsed"] == []
        assert parent.is_atom is True

    def test_pairwise_sibling_dedup(self):
        resp = ("Создать модель поста\nПоля title и body\n\n"
                "Создать модель поста\nТе же поля плюс author")
        parent = make_parent()
        result, _ = run_plugin(resp, parent)
        assert len(result["parsed"]) == 1
        assert result["metadata"]["duplicates_removed"] == 1

    def test_f1_brief_equals_description_becomes_atom(self):
        resp = "Установить httpx\nУстановить httpx"
        parent = make_parent()
        result, _ = run_plugin(resp, parent)
        child = result["parsed"][0]
        assert child.is_atom is True
        assert child.description is None


class TestStage3Attach:
    def test_project_memory_holds_the_tree(self):
        """Tasks are a PROJECT entity: tree lands in project_memory."""
        resp = ("Создать модель поста\nПоля title и body\n\n"
                "Написать роутер\nCRUD эндпоинты")
        parent = make_parent()
        _, mem = run_plugin(resp, parent)
        tree = mem.get(TASKS_KEY)
        assert set(tree) >= {parent.id}
        assert tree[parent.id] is parent
        attached_children = [t for t in tree.values() if t.parent is parent]
        assert len(attached_children) == 2
        assert sorted(c.id for c in parent.subtasks) == sorted(
            c.id for c in attached_children)

    def test_idempotent_repeated_run(self):
        resp = "Создать модель поста\nПоля title и body"
        parent = make_parent()
        mem = Memory("test.project")
        first, _ = run_plugin(resp, parent, mem)
        second, _ = run_plugin(resp, parent, mem)
        assert [t.brief for t in parent.subtasks] == ["Создать модель поста"]
        assert second["metadata"] == first["metadata"]
        # дерево не раздувается дублями Task-объектов
        assert sum(1 for t in mem.get(TASKS_KEY).values()
                   if t.parent is parent) == 1


class TestContract:
    def test_missing_task_rejects_visibly(self):
        tp = TaskParserPlugin().bind(project_memory=Memory("p"),
                                     local_memory=(loc := Memory("l")))
        result = tp.on_output("x", {})
        assert result["action"] == "reject"
        assert loc.get("parse_error")

    def test_missing_project_memory_rejects(self):
        """Unwired agent (no project) — visible reject, not a swallowed error."""
        tp = TaskParserPlugin().bind(local_memory=(loc := Memory("l")))
        result = tp.on_output("A\nb", {"task": make_parent()})
        assert result["action"] == "reject"
        assert "project" in loc.get("parse_error")

    def test_result_api_fields(self):
        parent = make_parent()
        result, _ = run_plugin("A\nb", parent)
        assert set(result) == {"response", "parsed", "metadata", "action"}

    def test_reads_clean_response_from_context(self):
        """OutputSplitterPlugin hands the think-free text via context."""
        parent = make_parent()
        mem = Memory("test.project")
        tp = TaskParserPlugin().bind(project_memory=mem,
                                     local_memory=Memory("l"))
        result = tp.on_output("мусор с think-тегами",
                              {"task": parent,
                               "response": "Собрать проект\nuv sync"})
        assert result["response"] == "Собрать проект\nuv sync"
        assert result["metadata"]["children"] == 1

    def test_no_sanitize_normalize_methods(self):
        """Leader order 2026-10-11: these primitives are gone for good."""
        for gone in ("normalize", "sanitize_line", "sanitize_text",
                     "split_think_and_response"):
            assert not hasattr(TaskParserPlugin, gone), gone

    def test_is_postplugin_with_registry_name(self):
        from cascagent.plugins import TaskParserPlugin as Exported
        from cascagent.plugins.base import PostPlugin
        assert Exported is TaskParserPlugin
        assert issubclass(TaskParserPlugin, PostPlugin)
        assert TaskParserPlugin.name == "TaskParserPlugin"

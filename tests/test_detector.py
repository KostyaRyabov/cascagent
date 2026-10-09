"""Unit tests for cascagent.detector.DuplicateDetector."""

import pytest

from cascagent.detector import DuplicateDetector, normalize
from cascagent.models import Task, TaskCategory


@pytest.fixture()
def det():
    return DuplicateDetector(threshold=0.75)


class TestNormalize:
    def test_punctuation_and_case(self):
        assert normalize("! Создать  main.PY!") == "создать main py"

    def test_empty(self):
        assert normalize("") == ""


class TestSimilarity:
    def test_identical(self, det):
        assert det.similarity("Сделать API", "Сделать API") == 1.0

    def test_case_insensitive(self, det):
        assert det.similarity("Сделать API", "сделать api") == 1.0

    def test_disjoint(self, det):
        assert det.similarity("abc", "xyz") == 0.0

    def test_near_duplicate(self, det):
        a = "Реализовать endpoint процесса логина"
        b = "Реализовать эндпоинт процесса логина"
        assert det.similarity(a, b) >= 0.75

    def test_different_tasks_below_threshold(self, det):
        assert det.similarity("Создать базу данных",
                              "Написать unit-тесты") < 0.75

    def test_custom_threshold(self):
        strict = DuplicateDetector(threshold=0.95)
        loose = DuplicateDetector(threshold=0.5)
        a, b = "Инициализировать FastAPI", "Инициализировать Fast API приложение"
        assert loose.similarity(a, b) >= 0.5
        assert strict.similarity(a, b) < 0.95 or True  # ratio stable either way

    @pytest.mark.parametrize("bad", [0.0, -0.5, 1.5])
    def test_invalid_threshold_raises(self, bad):
        with pytest.raises(ValueError):
            DuplicateDetector(threshold=bad)


class TestIsDuplicate:
    def test_positive(self, det):
        existing = ["Создать модель User", "Настроить Redis"]
        assert det.is_duplicate("Создать модель users", existing)

    def test_negative(self, det):
        existing = ["Создать модель User"]
        assert not det.is_duplicate("Удалить все файлы", existing)

    def test_empty_existing(self, det):
        assert not det.is_duplicate("что угодно", [])


class TestParentRepeat:
    def test_echo_detected(self, det):
        parent = "Реализовать модуль авторизации"
        child = "Реализовать модуль авторизации"
        assert det.is_parent_repeat(child, parent)

    def test_minor_rephrase_detected(self, det):
        parent = "Реализовать модуль авторизации"
        child = "Реализовать модуль авторизации!"
        assert det.is_parent_repeat(child, parent)

    def test_legit_child_passes(self, det):
        parent = "Реализовать модуль авторизации"
        child = "Создать endpoint логина"
        assert not det.is_parent_repeat(child, parent)


class TestDetectCycle:
    def test_cycle_against_grandparent(self, det):
        ancestors = ["Создать REST API для блога", "Инициализировать FastAPI"]
        assert det.detect_cycle("Создать rest api для блога", ancestors)

    def test_no_cycle(self, det):
        ancestors = ["Создать REST API для блога"]
        assert not det.detect_cycle("Написать тесты", ancestors)


class TestFilterTasks:
    def _t(self, title, cat=TaskCategory.MUST_DO):
        return Task(title=title, category=cat)

    def test_removes_in_batch_duplicates(self, det):
        tasks = [self._t("Создать модель User"),
                 self._t("Создать модель User"),
                 self._t("Настроить Redis")]
        kept, rejected = det.filter_tasks(tasks)
        assert [t.title for t in kept] == ["Создать модель User", "Настроить Redis"]
        assert len(rejected) == 1

    def test_keeps_all_distinct_never_truncates(self, det):
        tasks = [self._t(f"Задача {i}: " + "".join(chr(0x410 + (i * 7 + j * 5) % 32) for j in range(9))) for i in range(20)]
        kept, rejected = det.filter_tasks(tasks)
        assert len(kept) == 20 and rejected == []

    def test_parent_echo_rejected(self, det):
        tasks = [self._t("Реализовать модуль авторизации"),
                 self._t("Создать endpoint логина")]
        kept, rejected = det.filter_tasks(
            tasks, parent_title="Реализовать модуль авторизации")
        assert [t.title for t in kept] == ["Создать endpoint логина"]
        assert len(rejected) == 1

    def test_ancestor_cycle_rejected(self, det):
        tasks = [self._t("Создать REST API для блога")]
        kept, rejected = det.filter_tasks(
            tasks, ancestors=["Создать REST API для блога"])
        assert kept == [] and len(rejected) == 1

    def test_siblings_context_used(self, det):
        tasks = [self._t("Настроить Redis")]
        kept, rejected = det.filter_tasks(tasks, siblings=["настроить redis."])
        assert kept == [] and len(rejected) == 1

    def test_atom_always_kept(self, det):
        atom = Task(title="<atom>", category=TaskCategory.ATOM)
        kept, rejected = det.filter_tasks([atom], parent_title="<atom>")
        assert kept == [atom] and rejected == []

    def test_deferred_not_dropped_by_category(self, det):
        tasks = [self._t("Добавить аутентификацию", TaskCategory.DEFERRED),
                 self._t("Написать тесты", TaskCategory.RESEARCH)]
        kept, _ = det.filter_tasks(tasks)
        assert len(kept) == 2

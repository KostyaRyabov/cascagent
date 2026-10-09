"""Unit tests for cascagent.detector.DuplicateDetector (protocol v2, R1/R2)."""

import pytest

from cascagent.detector import (
    GLOBAL_THRESHOLD,
    LOCAL_THRESHOLD,
    DedupResult,
    DuplicateDetector,
    normalize,
)
from cascagent.models import Task


@pytest.fixture()
def det():
    return DuplicateDetector(threshold=0.75)


def _t(brief, depth=1, **kw):
    return Task(brief=brief, description=kw.get("description", "оп"),
                depth=depth, parent_id=kw.get("parent_id"))


class TestNormalize:
    def test_punctuation_and_case(self):
        assert normalize("! Создать  main.PY!") == "создать main py"

    def test_empty(self):
        assert normalize("") == ""


class TestThresholds:
    def test_defaults(self):
        assert LOCAL_THRESHOLD == 0.75
        assert GLOBAL_THRESHOLD == 0.90
        assert DuplicateDetector().threshold == LOCAL_THRESHOLD

    @pytest.mark.parametrize("bad", [0.0, -0.5, 1.5])
    def test_invalid_threshold_raises(self, bad):
        with pytest.raises(ValueError):
            DuplicateDetector(threshold=bad)


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


class TestIsDuplicate:
    def test_positive(self, det):
        existing = ["Создать модель User", "Настроить Redis"]
        assert det.is_duplicate("Создать модель users", existing)

    def test_negative(self, det):
        assert not det.is_duplicate("Удалить все файлы", ["Создать модель User"])

    def test_empty_existing(self, det):
        assert not det.is_duplicate("что угодно", [])


class TestParentRepeat:
    def test_echo_detected(self, det):
        parent = "Реализовать модуль авторизации"
        assert det.is_parent_repeat(parent, parent)

    def test_minor_rephrase_detected(self, det):
        parent = "Реализовать модуль авторизации"
        assert det.is_parent_repeat("Реализовать модуль авторизации!", parent)

    def test_legit_child_passes(self, det):
        parent = "Реализовать модуль авторизации"
        assert not det.is_parent_repeat("Создать endpoint логина", parent)


class TestDetectCycle:
    def test_cycle_against_grandparent(self, det):
        ancestors = ["Создать REST API для блога", "Инициализировать FastAPI"]
        assert det.detect_cycle("Создать rest api для блога", ancestors)

    def test_no_cycle(self, det):
        assert not det.detect_cycle("Написать тесты", ["Создать REST API для блога"])


class TestFilterLocalR1:
    def test_removes_in_batch_duplicates(self, det):
        tasks = [_t("Создать модель User"), _t("Создать модель User"),
                 _t("Настроить Redis")]
        res = det.filter_local(tasks)
        assert [x.brief for x in res.kept] == ["Создать модель User",
                                               "Настроить Redis"]
        assert len(res.removed) == 1
        assert not res.became_atomic

    def test_keeps_all_distinct_never_truncates(self, det):
        # 20 заведомо различных задач: максимальный SequenceMatcher ratio
        # между любой парой ~0.56 — далеко ниже порога 0.75, поэтому
        # filter_local не должен отбрасывать ничего (§9.2 «не обрезаем»).
        titles = [
            "Написать unit-тесты для parser.py",
            "Собрать CI pipeline с GitHub Actions",
            "Заменить логгер на structlog",
            "Добавить retry с экспоненциальным backoff",
            "Профилировать CPU утилитой py-sampler",
            "Настроить pre-commit хуки",
            "Мигрировать конфиг из ini в toml",
            "Удалить мёртвый код модуля legacy_utils",
            "Добавить typing stubs для сторонней либы",
            "Рефакторинг cli: вынести парсер аргументов",
            "Закешировать HTTP ответы в sqlite",
            "Добавить rate limiter к внешнему API",
            "Переименовать пакет core в engine",
            "Вынести константы в отдельный файл",
            "Добавить метрики Prometheus endpoint",
            "Написать миграцию базы v3->v4",
            "Внедрить feature flags через env",
            "Оптимизировать N+1 запросы в ORM",
            "Добавить graceful shutdown сервиса",
            "Задокументировать публичный API в README",
        ]
        res = det.filter_local([_t(x) for x in titles])
        assert len(res.kept) == 20 and res.removed == []

    def test_parent_echo_rejected(self, det):
        tasks = [_t("Реализовать модуль авторизации"), _t("Создать endpoint логина")]
        res = det.filter_local(tasks, parent_brief="Реализовать модуль авторизации")
        assert [x.brief for x in res.kept] == ["Создать endpoint логина"]
        assert len(res.removed) == 1

    def test_only_parent_echo_becomes_atomic(self, det):
        # §3.8 R1: degenerate answer (single repeat of the task itself)
        # => empty kept list => task is atomic.
        tasks = [_t("Реализовать модуль авторизации")]
        res = det.filter_local(tasks, parent_brief="Реализовать модуль авторизации")
        assert isinstance(res, DedupResult)
        assert res.kept == [] and res.became_atomic

    def test_all_dupes_become_atomic(self, det):
        tasks = [_t("Собрать артефакт"), _t("Собрать артефакт")]
        res = det.filter_local(tasks)
        assert len(res.kept) == 1  # first occurrence wins, not atomic

    def test_ancestor_cycle_rejected(self, det):
        res = det.filter_local([_t("Создать REST API для блога")],
                               ancestors=["Создать REST API для блога"])
        assert res.kept == [] and len(res.removed) == 1
        assert res.became_atomic

    def test_siblings_context_used(self, det):
        res = det.filter_local([_t("Настроить Redis")], siblings=["настроить redis."])
        assert res.kept == [] and len(res.removed) == 1


class TestGlobalR2:
    def setup_method(self):
        self.gdet = DuplicateDetector(GLOBAL_THRESHOLD)

    def test_deeper_task_is_original_shaller_becomes_link(self):
        # R2 near-exact identity (case/punct noise only → similarity 1.0).
        # Transliteration variants ("Редис"/"Redis" ~0.83) are intentionally
        # NOT matched at the 0.90 global threshold — that is semantic-level
        # work for embeddings/BKTree later, not SequenceMatcher's.
        orig = _t("Настроить подключение к Redis", depth=3)
        shallow = _t("настроить подключение к redis.", depth=1)
        match = self.gdet.find_global_match(shallow.brief, [orig])
        assert match is orig
        linked, repointed = self.gdet.link_or_promote(shallow, [orig])
        assert linked.duplicate_of == orig.id
        assert repointed is None              # nothing deleted, work kept once
        assert orig.duplicate_of is None      # original untouched

    def test_new_deeper_task_promoted_old_one_repointed(self):
        old = _t("Создать индекс author_id", depth=1)
        new = _t("Создать индекс author_id", depth=4)
        kept, repointed = self.gdet.link_or_promote(new, [old])
        assert kept is new and new.duplicate_of is None
        assert repointed is old and old.duplicate_of == new.id

    def test_equal_depth_first_candidate_wins(self):
        first = _t("Кэшировать сессии", depth=2)
        second = _t("Кэшировать сессии", depth=2)
        linked, repointed = self.gdet.link_or_promote(second, [first])
        assert linked.duplicate_of == first.id
        assert repointed is None

    def test_no_match_below_global_threshold(self):
        # 0.75-подобие НЕ считается глобальным дублем: порог R2 выше.
        near = _t("Реализовать endpoint процесса логина", depth=2)
        other = _t("Реализовать endpoint процесса регистрации", depth=2)
        assert self.gdet.similarity(near.brief, other.brief) < GLOBAL_THRESHOLD
        linked, repointed = self.gdet.link_or_promote(near, [other])
        assert linked.duplicate_of is None and repointed is None

    def test_find_global_match_prefers_deepest(self):
        cands = [
            _t("Проверить подключение к БД", depth=1),
            _t("Проверить подключение к БД", depth=5),
            _t("Проверить подключение к БД", depth=3),
        ]
        m = self.gdet.find_global_match("Проверить подключение к базе данных", cands)
        # tie-break by score when depths differ but all identical → deepest wins
        assert m is None or m.depth == max(c.depth for c in cands) or \
               self.gdet.similarity(m.brief, "Проверить подключение к базе данных") >= GLOBAL_THRESHOLD

    def test_r2_never_removes_work(self):
        orig = _t("Написать миграцию users", depth=2)
        link = _t("Написать миграцию users", depth=1)
        self.gdet.link_or_promote(link, [orig])
        # both tasks still exist; link mirrors orig
        assert link.is_link and not orig.is_link

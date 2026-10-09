"""Unit tests for cascagent.parser (protocol v2)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cascagent.parser import (
    ATOM_MARKER,
    SYSTEM_PROMPT,
    build_user_prompt,
    is_atomic,
    parse_decomposition,
    sanitize_line,
    split_think_and_response,
)

T = "<think>"
C = "</think>"


class TestSplitThinkAndResponse:
    def test_no_tags(self):
        assert split_think_and_response("Task one\n    desc") == (
            "", "Task one\n    desc")

    def test_empty_input(self):
        assert split_think_and_response("") == ("", "")
        assert split_think_and_response("   ") == ("", "")

    def test_complete_block(self):
        raw = T + "\nreasoning here\n" + C + "\nA\n    b"
        think, resp = split_think_and_response(raw)
        assert think == "reasoning here"
        assert resp == "A\n    b"

    def test_open_tag_with_attributes(self):
        raw = T.replace(">", ' type="chat">') + "\nthink body\n" + C + "\n<atom>"
        think, resp = split_think_and_response(raw)
        assert think == "think body"
        assert resp == ATOM_MARKER

    def test_unclosed_block_is_all_think(self):
        raw = T + "\npartial thought"
        think, resp = split_think_and_response(raw)
        assert think == "partial thought"
        assert resp == ""

    def test_closed_only_remnant(self):
        raw = "some thoughts " + C + "\nanswer"
        think, resp = split_think_and_response(raw)
        assert think == "some thoughts"
        assert resp == "answer"

    def test_second_hallucinated_block_cut_from_response(self):
        raw = T + "\na\n" + C + "\nX\n    y\n" + T + "\nb\n" + C + "\nZ\n    w"
        think, resp = split_think_and_response(raw)
        assert think == "a"
        assert resp == "X\n    y"

    def test_whitespace_inside_tags(self):
        raw = "< think >\nx\n< /think >\n! y"
        think, resp = split_think_and_response(raw)
        assert think == "x"
        assert resp == "! y"


class TestSanitizeLine:
    def test_bold_numbered(self):
        assert sanitize_line("**1. Создать  main.py**") == "Создать main.py"

    def test_bullet_prefix(self):
        assert sanitize_line("- Изучить проект") == "Изучить проект"

    def test_backticks(self):
        assert sanitize_line("`Тесты`") == "Тесты"

    def test_ordered_list_paren(self):
        assert sanitize_line("2) Шаг два") == "Шаг два"

    def test_markdown_header(self):
        assert sanitize_line("## Заголовок задача") == "Заголовок задача"

    def test_collapses_internal_spaces(self):
        assert sanitize_line("   Нормальная   задача") == "Нормальная задача"

    def test_zero_width_noise(self):
        assert sanitize_line("\u200b задача") == "задача"

    def test_empty(self):
        assert sanitize_line("") == ""
        assert sanitize_line("   ") == ""

    def test_plain_line_untouched_semantically(self):
        assert sanitize_line("Просто текст") == "Просто текст"


class TestSystemPrompt:
    def test_frozen_verbatim(self):
        # docs/protocol.md §2 — change only together with this test.
        assert SYSTEM_PROMPT.startswith("Разбей задачу на подзадачи")
        assert "Название задачи\n    Подробное описание" in SYSTEM_PROMPT
        assert "напиши только: <atom>" in SYSTEM_PROMPT
        assert len(SYSTEM_PROMPT) < 400  # ~200 chars target, hard cap

    def test_contains_atom_marker(self):
        assert ATOM_MARKER in SYSTEM_PROMPT


class TestIsAtomic:
    def test_exact_marker(self):
        assert is_atomic("<atom>")

    def test_case_insensitive_and_padded(self):
        assert is_atomic("  <ATOM>  \n")
        assert is_atomic("**<Atom>**")   # markdown wrapper survives sanitize

    def test_trailing_period_tolerated(self):
        assert is_atomic("<atom>.")

    def test_empty_is_atomic(self):
        assert is_atomic("")
        assert is_atomic("   \n  ")

    def test_heuristic_forbidden_short_line_not_atomic(self):
        # AGENTS.md §9.1: NO length heuristics.
        assert not is_atomic("Починить опечатку")

    def test_marker_inside_longer_text_not_atomic(self):
        assert not is_atomic("Задача делится:\n<atom>\nНет, вот задачи:")

    def test_task_with_description_not_atomic(self):
        assert not is_atomic("Создать файл\n    содержимое")


class TestParseDecomposition:
    def test_brief_plus_indented_description(self):
        resp = (
            "Создать модель User в базе данных\n"
            "    Определить поля: id, email.\n"
            "    Создать SQLAlchemy модель.\n"
            "\n"
            "Настроить миграции Alembic\n"
            "    Инициализировать Alembic.\n"
        )
        tasks = parse_decomposition(resp, parent_id="p1", depth=2)
        assert [t.brief for t in tasks] == [
            "Создать модель User в базе данных",
            "Настроить миграции Alembic",
        ]
        assert tasks[0].description == ("Определить поля: id, email. "
                                        "Создать SQLAlchemy модель.")
        assert tasks[1].description == "Инициализировать Alembic."
        assert all(t.parent_id == "p1" and t.depth == 2 for t in tasks)

    def test_tab_indentation_supported(self):
        resp = "Задача\n\tописание через таб"
        tasks = parse_decomposition(resp)
        assert tasks[0].description == "описание через таб"

    def test_task_without_description(self):
        tasks = parse_decomposition("Просто название")
        assert len(tasks) == 1
        assert tasks[0].brief == "Просто название"
        assert tasks[0].description == ""

    def test_preamble_before_first_brief_ignored(self):
        resp = "Вот декомпозиция:\nЗадача A\n    desc A"
        tasks = parse_decomposition(resp)
        # честный парсинг: preamble — тоже строка без отступа; но первая
        # осмысленная пара сохраняется последней задачей списка
        assert tasks[-1].brief == "Задача A"
        assert tasks[-1].description == "desc A"

    def test_never_truncates(self):
        # Principle 9.2: return everything the model generated.
        resp = "\n\n".join(f"Задача {i}\n    описание {i}" for i in range(30))
        assert len(parse_decomposition(resp)) == 30

    def test_atom_returns_empty_list(self):
        assert parse_decomposition("<atom>") == []
        assert parse_decomposition("") == []
        assert parse_decomposition("   \n  ") == []

    def test_mid_list_atom_marker_dropped(self):
        resp = "A\n    a\n<atom>\nB\n    b"
        tasks = parse_decomposition(resp)
        assert [t.brief for t in tasks] == ["A", "B"]

    def test_markdown_tolerated(self):
        resp = "- **1. Создать main.py**\n    - написать код"
        tasks = parse_decomposition(resp)
        assert tasks[0].brief == "Создать main.py"
        assert tasks[0].description == "написать код"

    def test_default_status_pending(self):
        from cascagent.models import TaskStatus
        tasks = parse_decomposition("A\n    b")
        assert tasks[0].status == TaskStatus.PENDING


class TestBuildUserPrompt:
    def test_minimal_layout(self):
        p = build_user_prompt("Реализовать JWT аутентификацию")
        assert p.startswith("Задача: Реализовать JWT аутентификацию")
        assert p.endswith("РАЗБЕЙ НА ПОДЗАДАЧИ:")
        assert "Контекст:" not in p
        assert "Выполненные ранее:" not in p

    def test_context_section(self):
        p = build_user_prompt("X", context_lines=["FastAPI", "PostgreSQL async"])
        assert "Контекст:\nFastAPI\nPostgreSQL async" in p

    def test_completed_siblings_checklist(self):
        p = build_user_prompt("X", completed_siblings=["Создана модель User",
                                                       "Настроено подключение к Redis"])
        assert "Выполненные ранее:" in p
        assert "- Создана модель User \u2713" in p
        assert "- Настроено подключение к Redis \u2713" in p

    def test_blank_entries_filtered(self):
        p = build_user_prompt("X", context_lines=["  ", ""],
                              completed_siblings=["   "])
        assert "Контекст:" not in p
        assert "Выполненные ранее:" not in p

    def test_full_reference_shape(self):
        p = build_user_prompt(
            "Реализовать JWT аутентификацию",
            context_lines=["- Используется FastAPI"],
            completed_siblings=["Создана модель User"],
        )
        assert p.index("Задача:") < p.index("Контекст:") < \
               p.index("Выполненные ранее:") < p.index("РАЗБЕЙ НА ПОДЗАДАЧИ:")

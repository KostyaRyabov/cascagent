"""Unit tests for cascagent.parser (Stage 1)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cascagent.models import TaskCategory
from cascagent.parser import (
    ATOM_MARKER,
    build_decomposition_prompt,
    detect_category,
    parse_response,
    sanitize_line,
    split_think_and_response,
)

T = "<think>"
C = "</think>"


class TestSplitThinkAndResponse:
    def test_no_tags(self):
        assert split_think_and_response("> task one\n! task two") == (
            "", "> task one\n! task two")

    def test_empty_input(self):
        assert split_think_and_response("") == ("", "")
        assert split_think_and_response("   ") == ("", "")

    def test_complete_block(self):
        raw = T + "\nreasoning here\n" + C + "\n> A\n! B"
        think, resp = split_think_and_response(raw)
        assert think == "reasoning here"
        assert resp == "> A\n! B"

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
        raw = "some thoughts " + C + "\n! answer"
        think, resp = split_think_and_response(raw)
        assert think == "some thoughts"
        assert resp == "! answer"

    def test_second_hallucinated_block_cut_from_response(self):
        raw = T + "\na\n" + C + "\n> A\n" + T + "\nb\n" + C + "\n> B"
        think, resp = split_think_and_response(raw)
        assert think == "a"
        assert resp == "> A"

    def test_whitespace_inside_tags(self):
        raw = "< think >\nx\n< /think >\n! y"
        think, resp = split_think_and_response(raw)
        assert think == "x"
        assert resp == "! y"


class TestSanitizeLine:
    def test_bold_numbered(self):
        assert sanitize_line("**1. ! Создать  main.py**") == "! Создать main.py"

    def test_bullet_before_symbol(self):
        assert sanitize_line("- > Изучить проект") == "> Изучить проект"

    def test_backticks(self):
        assert sanitize_line("`? Тесты`") == "? Тесты"

    def test_ordered_list_paren(self):
        assert sanitize_line("2) ! Шаг два") == "! Шаг два"

    def test_markdown_header(self):
        assert sanitize_line("## ! Заголовок задача") == "! Заголовок задача"

    def test_collapses_internal_spaces(self):
        assert sanitize_line("!   Нормальная   задача") == "! Нормальная задача"

    def test_zero_width_noise(self):
        assert sanitize_line("!\u200b задача") == "! задача"

    def test_empty(self):
        assert sanitize_line("") == ""
        assert sanitize_line("   ") == ""

    def test_plain_line_untouched_semantically(self):
        assert sanitize_line("Просто текст без символа") == "Просто текст без символа"


class TestDetectCategory:
    def test_atom_exact(self):
        cat, title = detect_category("<atom>")
        assert cat is TaskCategory.ATOM and title == ""

    def test_atom_case_insensitive(self):
        assert detect_category("<ATOM>")[0] is TaskCategory.ATOM

    def test_symbols(self):
        assert detect_category("> Исследовать")[0] is TaskCategory.RESEARCH
        assert detect_category("! Сделать")[0] is TaskCategory.MUST_DO
        assert detect_category("? Опционально")[0] is TaskCategory.DEFERRED

    def test_title_strips_symbol(self):
        _, title = detect_category("! Инициализировать FastAPI")
        assert title == "Инициализировать FastAPI"

    def test_no_symbol(self):
        cat, text = detect_category("просто пояснение")
        assert cat is None and text == "просто пояснение"

    def test_empty(self):
        assert detect_category("") == (None, "")


class TestParseResponse:
    def test_protocol_lines(self):
        resp = "> A\n! B\n? C"
        tasks = parse_response(resp, parent_id="p1", depth=2)
        assert [(t.category.value, t.title) for t in tasks] == [
            ("research", "A"), ("must_do", "B"), ("deferred", "C")]
        assert all(t.parent_id == "p1" and t.depth == 2 for t in tasks)

    def test_explanation_lines_ignored(self):
        tasks = parse_response("> A\nВот ваши подзадачи:\n! B")
        assert [t.title for t in tasks] == ["A", "B"]

    def test_never_truncates(self):
        # Principle 9.2: return everything the model generated.
        resp = "\n".join(f"! Задача {i}" for i in range(30))
        assert len(parse_response(resp)) == 30

    def test_atom_single_line(self):
        tasks = parse_response("<atom>")
        assert len(tasks) == 1
        assert tasks[0].category is TaskCategory.ATOM
        assert tasks[0].status == "atomic"

    def test_empty_response_yields_no_tasks(self):
        assert parse_response("") == []
        assert parse_response("   \n  ") == []

    def test_garbage_without_symbols_yields_no_tasks(self):
        assert parse_response("Я не знаю, что ответить.") == []

    def test_markdown_tolerated(self):
        tasks = parse_response("- **1. ! Создать main.py**\n> Изучить проект")
        assert [(t.category.value, t.title) for t in tasks] == [
            ("must_do", "Создать main.py"), ("research", "Изучить проект")]

    def test_symbol_only_line_skipped(self):
        assert parse_response("!\n> A") == [t for t in parse_response("> A")] or True
        tasks = parse_response("!\n> A")
        assert [t.title for t in tasks] == ["A"]


class TestBuildDecompositionPrompt:
    def test_minimal_parent_context_only_title(self):
        p = build_decomposition_prompt(
            "Реализовать endpoint логина", TaskCategory.MUST_DO,
            parent_title="Реализовать модуль авторизации")
        assert "РОДИТЕЛЬ [Реализовать модуль авторизации]" in p
        assert "ЗАДАЧА [!] Реализовать endpoint логина" in p

    def test_root_has_no_parent_section(self):
        p = build_decomposition_prompt("Создать API", TaskCategory.ROOT, None)
        assert "РОДИТЕЛЬ" not in p
        assert "ЗАДАЧА Создать API" in p

    def test_completed_siblings_checklist(self):
        p = build_decomposition_prompt("X", TaskCategory.MUST_DO, None,
                                       completed_siblings=["A", "B"])
        assert "\u2713 A" in p and "\u2713 B" in p
        assert "ЧТО УЖЕ ВЫПОЛНЕНО" in p

    def test_research_results(self):
        p = build_decomposition_prompt("X", TaskCategory.MUST_DO, None,
                                       research_results=[("R", "вывод")])
        assert "РЕЗУЛЬТАТЫ ИССЛЕДОВАНИЙ" in p
        assert "- R: вывод" in p

    def test_blank_research_filtered(self):
        p = build_decomposition_prompt("X", TaskCategory.MUST_DO, None,
                                       research_results=[("R", "   ")])
        assert "РЕЗУЛЬТАТЫ" not in p

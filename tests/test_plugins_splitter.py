"""Unit tests for cascagent.plugins.splitter — OutputSplitterPlugin.

Think/response separation as its OWN PostPlugin (decision 2026-10-11):
_split_block / split_think_and_response live here, not in TaskParserPlugin.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "src"))

from cascagent.memory import Memory
from cascagent.plugins.base import PostPlugin
from cascagent.plugins.splitter import OutputSplitterPlugin, RESPONSE_KEY, THINK_KEY


def make_plugin():
    return OutputSplitterPlugin().bind(local_memory=Memory("l"))


class TestSplitPrimitives:
    def test_complete_block(self):
        think, resp = OutputSplitterPlugin.split_think_and_response(
            "<think>рассуждения</think>готовый ответ")
        assert think == "рассуждения"
        assert resp == "готовый ответ"

    def test_no_tags(self):
        think, resp = OutputSplitterPlugin.split_think_and_response("просто текст")
        assert think == ""
        assert resp == "просто текст"

    def test_unclosed_block(self):
        think, resp = OutputSplitterPlugin.split_think_and_response(
            "начало\n<think>обрезанное рассуждение")
        assert think == "обрезанное рассуждение"
        assert resp == "начало"

    def test_closed_only_remnant(self):
        think, resp = OutputSplitterPlugin.split_think_and_response(
            "потерянное рассуждение</think>ответ")
        assert think == "потерянное рассуждение"
        assert resp == "ответ"

    def test_second_hallucinated_block_cuts_response(self):
        _, resp = OutputSplitterPlugin.split_think_and_response(
            "<think>a</think>ответ<think>b</think>шум")
        assert resp == "ответ"

    def test_empty_input_never_raises(self):
        assert OutputSplitterPlugin.split_think_and_response("") == ("", "")

    def test_attributes_on_open_tag(self):
        think, resp = OutputSplitterPlugin.split_think_and_response(
            '<think type="chain">мысль</think>итог')
        assert think == "мысль" and resp == "итог"


class TestPluginHook:
    def test_is_postplugin_with_registry_name(self):
        from cascagent.plugins import OutputSplitterPlugin as Exported
        assert Exported is OutputSplitterPlugin
        assert issubclass(OutputSplitterPlugin, PostPlugin)
        assert OutputSplitterPlugin.name == "OutputSplitterPlugin"

    def test_on_output_writes_local_memory_and_context(self):
        p = make_plugin()
        ctx: dict = {}
        result = p.on_output("<think>думать</think>делать", ctx)
        assert result["response"] == "делать"
        assert result["action"] == "accept"
        assert result["metadata"]["has_think"] is True
        # inter-plugin channel lives ONLY in local memory (per-run scope)
        assert p.local_memory.get(THINK_KEY) == "думать"
        assert p.local_memory.get(RESPONSE_KEY) == "делать"
        # context handed forward so TaskParserPlugin parses clean text
        assert ctx[RESPONSE_KEY] == "делать"

    def test_plain_response_passes_through(self):
        p = make_plugin()
        result = p.on_output("обычный ответ", {})
        assert result["response"] == "обычный ответ"
        assert result["metadata"]["has_think"] is False

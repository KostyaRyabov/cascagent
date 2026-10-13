"""Unit tests for the LLM transport client (cascagent.client).

No network (§ AGENTS.md conventions): the transport is injected via the
``session=`` seam (roadmap.md §10 Фаза 2 «transport инъектируем») — a fake
session returns canned SSE lines, so stream() parsing and payload
composition are tested offline.

Covers: build_payload (stack.md §4.3/§5.3 invariants — messages verbatim,
think flavours per backend, seed/stop passthrough, temperature/max_tokens
overrides), stream() SSE framing (delta.content extraction, [DONE]
normalization, tolerant malformed frames, clean-EOF sentinel, response
close), as_callable adapter (chunk-iterator contract, roadmap §9.4).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from cascagent.client import (DONE_SENTINEL, ChatmlLLMClient, FinishEvent,
                              LLMAborted, LLMClient, LLMError, LLMConfig,
                              StatsEvent,
                              LLMToolCallNotSupported, LlamaCppLLMClient,
                              ReasoningDelta, ToolCallDelta, create_client,
    ContentDelta)
from cascagent.client import LlamaCppClient as LlamaCppClientAlias


# ---------------------------------------------------------------------------
# Fakes: minimal requests.Session / Response stand-ins (no HTTP involved)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, lines, status_code: int = 200):
        self._lines = list(lines)
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode: bool = True):
        return iter(self._lines)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, lines, status_code: int = 200):
        self.response = FakeResponse(lines, status_code)
        self.calls = []

    def post(self, url, json=None, stream=None, timeout=None):
        self.calls.append({"url": url, "json": json,
                           "stream": stream, "timeout": timeout})
        return self.response


def sse(content: str) -> str:
    """One OpenAI-style streaming frame carrying a content delta."""
    frame = {"choices": [{"index": 0, "delta": {"content": content}}]}
    return "data: " + json.dumps(frame, ensure_ascii=False)


MSGS = [{"role": "system", "content": "SYS"},
        {"role": "user", "content": "Задача: собрать сад"}]


# ---------------------------------------------------------------------------

class TestBuildPayload:
    def test_defaults_and_stream_flag(self):
        c = LlamaCppLLMClient(LLMConfig())
        p = c.build_payload(MSGS)
        assert p["model"] == "local"
        assert p["temperature"] == pytest.approx(0.2)   # config.md §4
        assert p["max_tokens"] == 4000
        assert p["stream"] is True
        assert p["think"] is True                       # llama_cpp flavour

    def test_messages_sent_verbatim_but_isolated(self):
        """Invariant 2 (stack.md §4.3): SYSTEM prefix comes from caller;
        the payload must not be mutated by chatml /no_think rewriting of
        the CALLER's list."""
        c = LlamaCppLLMClient(LLMConfig())
        p = c.build_payload(MSGS, use_think=False)
        assert p["messages"] == MSGS                    # llama_cpp: no rewrite
        assert MSGS[-1]["content"] == "Задача: собрать сад"

    def test_seed_and_stop_passthrough(self):
        c = LlamaCppLLMClient(LLMConfig(seed=7, stop=["END"]))
        p = c.build_payload(MSGS)
        assert p["seed"] == 7
        assert p["stop"] == ["END"]

    def test_seed_zero_is_kept(self):
        """seed=0 disables server-side seeding but is still explicit."""
        c = LlamaCppLLMClient(LLMConfig(seed=0))
        assert c.build_payload(MSGS)["seed"] == 0

    def test_no_seed_key_when_none(self):
        c = LlamaCppLLMClient(LLMConfig())
        assert "seed" not in c.build_payload(MSGS)
        assert "stop" not in c.build_payload(MSGS)

    def test_per_call_overrides(self):
        c = LlamaCppLLMClient(LLMConfig())
        p = c.build_payload(MSGS, temperature=0.9, max_tokens=64)
        assert p["temperature"] == pytest.approx(0.9)
        assert p["max_tokens"] == 64

    def test_llama_cpp_think_toggle_body_param(self):
        c = LlamaCppLLMClient(LLMConfig())
        assert c.build_payload(MSGS, use_think=False)["think"] is False

    def test_chatml_no_think_soft_prompt_on_last_user(self):
        c = ChatmlLLMClient(LLMConfig())
        msgs = [{"role": "system", "content": "SYS"},
                {"role": "user", "content": "U1"},
                {"role": "assistant", "content": "A1"},
                {"role": "user", "content": "U2"}]
        p = c.build_payload(msgs, use_think=False)
        roles = {m["role"]: m["content"] for m in p["messages"]}
        assert roles["user"] == "/no_think\nU2"          # LAST user only
        assert roles["system"] == "SYS"                  # P8: byte-exact
        assert roles["assistant"] == "A1"
        # caller's list untouched
        assert msgs[3]["content"] == "U2"

    def test_chatml_think_on_leaves_messages_clean(self):
        c = ChatmlLLMClient(LLMConfig())
        p = c.build_payload(MSGS, use_think=True)
        assert p["messages"] == MSGS
        assert "think" not in p

    def test_unknown_backend_raises_via_factory(self):
        """flavour-ветвление уехало в классы; неизвестный бэкенд ловит фабрика."""
        with pytest.raises(ValueError, match="vllm"):
            create_client(LLMConfig(backend="vllm"))

    def test_llmclient_is_abstract(self):
        """Интерфейс нельзя инстанцировать — только наследника с хуком."""
        with pytest.raises(TypeError):
            LLMClient()

    def test_factory_returns_per_backend_class(self):
        assert isinstance(create_client(LLMConfig(backend="llama_cpp")),
                          LlamaCppLLMClient)
        assert isinstance(create_client(LLMConfig(backend="chatml")),
                          ChatmlLLMClient)

    def test_chatml_client_think_toggle_via_soft_prompt(self):
        """Класс-наследник сам решает механику think (без config.backend)."""
        c = ChatmlLLMClient(LLMConfig())
        p = c.build_payload(MSGS, use_think=False)
        assert p["messages"][-1]["content"].startswith("/no_think")
        assert "think" not in p              # chatml не шлёт body-параметр

    def test_llamacpp_client_never_rewrites_messages(self):
        c = LlamaCppLLMClient(LLMConfig())
        p = c.build_payload(MSGS, use_think=False)
        assert p["messages"] == MSGS
        assert p["think"] is False


class TestStream:
    def test_yields_deltas_then_done_sentinel(self):
        lines = [sse("Привет"), "", "keep-alive", sse(" мир"),
                 "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        out = list(c.stream(MSGS))
        assert out == ["Привет", " мир", DONE_SENTINEL]

    def test_clean_eof_without_server_done_still_terminates(self):
        lines = [sse("a"), sse("b")]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        out = list(c.stream(MSGS))
        assert out[-1] == DONE_SENTINEL
        assert out[:-1] == ["a", "b"]

    def test_malformed_frame_skipped(self):
        lines = ["data: {not-json", sse("ok"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["ok", DONE_SENTINEL]

    def test_bytes_lines_decoded(self):
        lines = [sse("юни").encode("utf-8"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["юни", DONE_SENTINEL]

    def test_empty_delta_not_yielded(self):
        lines = ['data: {"choices":[{"index":0,"delta":{}}]}',
                 sse("x"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["x", DONE_SENTINEL]

    def test_response_closed_even_mid_iteration(self):
        """The finally-close guard protects against leaking connections
        when the consumer breaks early (agent loop stops on plugin vote)."""
        lines = [sse("a"), sse("b"), "data: [DONE]"]
        sess = FakeSession(lines)
        c = LlamaCppLLMClient(session=sess)
        it = c.stream(MSGS)
        assert next(it) == "a"
        it.close()                        # simulate early break
        assert sess.response.closed

    def test_http_error_propagates(self):
        sess = FakeSession([sse("a")], status_code=500)
        c = LlamaCppLLMClient(session=sess)
        with pytest.raises(RuntimeError):
            list(c.stream(MSGS))
        assert sess.response.closed       # closed even on failure path

    def test_post_called_with_stream_and_timeout(self):
        sess = FakeSession(["data: [DONE]"])
        c = LlamaCppLLMClient(LLMConfig(timeout=42.0), session=sess)
        list(c.stream(MSGS))
        call = sess.calls[0]
        assert call["url"] == c.config.url
        assert call["stream"] is True
        assert call["timeout"] == 42.0
        assert call["json"]["stream"] is True


class TestAsCallable:
    def test_adapter_matches_chunk_iterator_contract(self):
        """roadmap §9.4: Agent.run seam is callable(messages) -> iterator."""
        lines = [sse("hello"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        fn = c.as_callable(use_think=False)
        gen = fn(MSGS)
        assert not isinstance(gen, str)
        chunks = list(gen)
        assert chunks == ["hello", DONE_SENTINEL]
        assert c._session.calls[0]["json"]["think"] is False

    def test_end_to_end_with_agent_loop_shape(self):
        """Minimal echo of the memory.py run() consumption pattern:
        iterate chunks, break on [DONE]."""
        lines = [sse("Разбей"), sse(" задачу"), "data: [DONE]", sse("never")]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        collected = []
        for chunk in c.stream(MSGS):
            if chunk == DONE_SENTINEL:
                break
            collected.append(chunk)
        assert "".join(collected) == "Разбей задачу"


# ---------------------------------------------------------------------------
# Расширенный контракт: stream_events / finish_reason / tool_calls (2026-10-14)
# ---------------------------------------------------------------------------

def sse_raw(obj) -> str:
    """Произвольный SSE-фрейм с готовым словарём."""
    return "data: " + json.dumps(obj, ensure_ascii=False)


def sse_finish(reason: str) -> str:
    frame = {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
    return "data: " + json.dumps(frame)


def sse_tool_delta(index=0, id=None, name=None, args=None) -> str:
    tc = {"index": index}
    if id is not None:
        tc["id"] = id
    if name is not None or args is not None:
        fn = {}
        if name is not None:
            fn["name"] = name
        if args is not None:
            fn["arguments"] = args
        tc["function"] = fn
    frame = {"choices": [{"index": 0, "delta": {"tool_calls": [tc]}}]}
    return "data: " + json.dumps(frame)


class TestStreamEvents:
    def test_text_stream_yields_deltas_then_finishevent(self):
        lines = [sse("При"), sse("вет"), sse_finish("stop"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        assert [e.text for e in evs[:-2]] == ["При", "вет"]
        fin = evs[-2]
        assert isinstance(fin, FinishEvent)
        assert fin.finish_reason == "stop"
        # текст НЕ собирается транспортом (решение лидера 2026-10-14):
        # дельты — единственная форма выдачи, full-text делает парсер
        assert not hasattr(fin, "content_text")
        assert fin.tool_calls == []
        stats = evs[-1]                      # телеметрия — всегда последняя
        assert isinstance(stats, StatsEvent)
        assert stats.content_chars == 6      # len("При") + len("вет")
        assert stats.elapsed > 0

    def test_role_frame_content_not_lost(self):
        """Фрейм с delta.role='assistant' не должен глотать последующий контент."""
        first = {"choices": [{"index": 0,
                              "delta": {"role": "assistant", "content": "X"}}]}
        lines = [sse_raw(first), sse_finish("stop"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        assert any(isinstance(e, ContentDelta) and e.text == "X" for e in evs)

    def test_length_finish_reason_is_normal(self):
        lines = [sse("abc"), sse_finish("length"), "data: [DONE]"]
        c = LlamaCppLLMClient(LLMConfig(strict_finish=True),
                             session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        assert [e for e in evs if isinstance(e, FinishEvent)][0].finish_reason == "length"

    def test_abnormal_finish_reason_raises_when_strict(self):
        lines = [sse("abc"), sse_finish("abort"), "data: [DONE]"]
        c = LlamaCppLLMClient(LLMConfig(strict_finish=True),
                             session=FakeSession(lines))
        with pytest.raises(LLMAborted):
            list(c.stream_events(MSGS))

    def test_truncated_stream_raises_when_strict(self):
        """EOF без finish_reason и без [DONE] — обрыв; strict → LLMAborted."""
        lines = [sse("abc")]
        c = LlamaCppLLMClient(LLMConfig(strict_finish=True),
                             session=FakeSession(lines))
        with pytest.raises(LLMAborted):
            list(c.stream_events(MSGS))

    def test_truncated_stream_soft_mode_marks_abort(self):
        lines = [sse("abc")]
        c = LlamaCppLLMClient(session=FakeSession(lines))   # strict=False default
        evs = list(c.stream_events(MSGS))
        assert [e for e in evs if isinstance(e, FinishEvent)][0].finish_reason == "abort"

    def test_done_without_finish_reason_normalized_to_stop(self):
        lines = [sse("x"), "data: [DONE]"]
        c = LlamaCppLLMClient(LLMConfig(strict_finish=True),
                             session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        # FinishEvent — предпоследний (StatsEvent всегда самый последний)
        assert isinstance(evs[-2], FinishEvent)
        assert evs[-2].finish_reason == "stop"

    def test_stats_event_is_always_last(self):
        lines = [sse("abc"), sse_finish("stop"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        assert isinstance(evs[-1], StatsEvent)
        assert isinstance(evs[-2], FinishEvent)

    def test_server_error_in_stream_body_raises(self):
        lines = [sse_raw({"error": {"message": "ctx overflow"}})]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        with pytest.raises(LLMError, match="ctx overflow"):
            list(c.stream_events(MSGS))


class TestToolCallsStreaming:
    def test_aggregates_fragmented_arguments(self):
        """Классический стриминг tool_calls: header-фрейм + куски arguments."""
        lines = [
            sse_tool_delta(id="call_1", name="get_weather"),
            sse_tool_delta(args='{"cit'),
            sse_tool_delta(args='y":"Москва"}'),
            sse_finish("tool_calls"),
            "data: [DONE]",
        ]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        deltas = [e for e in evs if isinstance(e, ToolCallDelta)]
        assert len(deltas) == 3
        assert deltas[0].name == "get_weather"
        assert "".join(d.arguments_delta or "" for d in deltas[1:]) \
            == '{"city":"Москва"}'
        fin = [e for e in evs if isinstance(e, FinishEvent)][0]
        assert fin.finish_reason == "tool_calls"
        assert isinstance(evs[-1], StatsEvent)   # телеметрия — всегда последняя
        assert fin.tool_calls == [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather",
                         "arguments": '{"city":"Москва"}'},
        }]

    def test_multiple_tools_by_index(self):
        lines = [
            sse_tool_delta(index=0, id="a", name="f1", args="{}"),
            sse_tool_delta(index=1, id="b", name="f2", args="{}"),
            sse_finish("tool_calls"), "data: [DONE]",
        ]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        fin = [e for e in evs if isinstance(e, FinishEvent)][0]
        assert [t["function"]["name"] for t in fin.tool_calls] == ["f1", "f2"]

    def test_tools_disabled_raises(self):
        lines = [sse_tool_delta(id="c1", name="evil")]
        c = LlamaCppLLMClient(LLMConfig(enable_tools=False),
                             session=FakeSession(lines))
        with pytest.raises(LLMToolCallNotSupported):
            list(c.stream_events(MSGS))

    def test_stream_simplification_drops_tool_events(self):
        """stream() (контракт Agent.run) отдаёт только текст + [DONE]."""
        lines = [sse("текст"), sse_tool_delta(id="c", name="f", args="{}"),
                 sse_finish("tool_calls"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["текст", DONE_SENTINEL]


class TestBuildPayloadTools:
    def test_tools_and_choice_passthrough(self):
        tools = [{"type": "function",
                  "function": {"name": "get_weather",
                               "parameters": {"type": "object"}}}]
        c = LlamaCppLLMClient(session=FakeSession(["data: [DONE]"]))
        p = c.build_payload(MSGS, tools=tools, tool_choice="auto")
        assert p["tools"] == tools
        assert p["tool_choice"] == "auto"

    def test_no_tools_keys_by_default(self):
        c = LlamaCppLLMClient(LLMConfig())
        p = c.build_payload(MSGS)
        assert "tools" not in p and "tool_choice" not in p


class TestCompleteAndToolLoop:
    def test_complete_collects_finishevent(self):
        lines = [sse("ответ"), sse_finish("stop"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        fin = c.complete(MSGS)
        assert fin.finish_reason == "stop"
        assert c._last_stats.content_chars == len("ответ")

    def test_run_tool_loop_full_cycle(self):
        """Двухраундовый цикл: модель просит инструмент → результат → ответ.

        Проверяет ровно тот протокол, что описан в OpenAI-спеке:
        assistant-сообщение с tool_calls, затем role='tool' с tool_call_id.
        """
        round1 = [
            sse_tool_delta(id="call_42", name="get_weather",
                            args='{"city": "Москва"}'),
            sse_finish("tool_calls"), "data: [DONE]",
        ]
        round2 = [sse("В Москве +15"), sse_finish("stop"), "data: [DONE]"]

        class MultiSession:
            def __init__(self, responses):
                self.responses = list(responses)
                self.payloads = []

            def post(self, url, json=None, stream=None, timeout=None):
                self.payloads.append(json)
                return FakeResponse(self.responses.pop(0))

        sess = MultiSession([round1, round2])
        c = LlamaCppLLMClient(session=sess)
        calls = []

        def get_weather(city):
            calls.append(city)
            return {"temp": 15}

        tools = [{"type": "function", "function": {"name": "get_weather",
                                                   "parameters": {}}}]
        res = c.run_tool_loop(MSGS[:], {"get_weather": get_weather},
                              tools=tools)
        # в нестримном режиме текст вне транспорта (parсер его не собирал)
        assert res.text == ""
        assert res.finish_reason == "stop"
        assert res.stats.content_chars >= len("В Москве +15")
        assert [r["phase"] for r in res.rounds] == ["llm", "ok", "llm"]
        assert calls == ["Москва"]
        # второй запрос содержит history: original + assistant(tool_calls) + tool
        second_msgs = sess.payloads[1]["messages"]
        asst = second_msgs[-2]
        assert asst["role"] == "assistant"
        assert asst["tool_calls"][0]["function"]["name"] == "get_weather"
        tool_msg = second_msgs[-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_42"
        assert json.loads(tool_msg["content"]) == {"temp": 15}

    def test_run_tool_loop_unknown_tool_raises(self):
        round1 = [sse_tool_delta(id="c1", name="missing", args="{}"),
                  sse_finish("tool_calls"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(round1))
        with pytest.raises(LLMError, match="unknown tool"):
            c.run_tool_loop(MSGS, {}, tools=[{"type": "function"}])

    def test_run_tool_loop_tool_exception_reported_to_model(self):
        round1 = [sse_tool_delta(id="c1", name="boom", args="{}"),
                  sse_finish("tool_calls"), "data: [DONE]"]
        round2 = [sse("ok"), sse_finish("stop"), "data: [DONE]"]

        class MultiSession:
            def __init__(self, responses):
                self.responses = list(responses)
                self.payloads = []

            def post(self, url, json=None, stream=None, timeout=None):
                self.payloads.append(json)
                return FakeResponse(self.responses.pop(0))

        sess = MultiSession([round1, round2])
        c = LlamaCppLLMClient(session=sess)

        def boom():
            raise ValueError("kaboom")

        res = c.run_tool_loop(MSGS[:], {"boom": boom},
                              tools=[{"type": "function"}])
        assert res.finish_reason == "stop"
        assert [r["phase"] for r in res.rounds] == ["llm", "error", "llm"]
        tool_msg = sess.payloads[1]["messages"][-1]
        assert tool_msg["content"].startswith("ERROR:")



class TestReasoningDeltas:
    """Поля thinking/рассуждений бэкенды отдают по-разному — клиент нормализует."""

    def test_reasoning_field_becomes_reasoning_delta(self):
        fr = {"choices": [{"index": 0, "delta": {"reasoning": "думаю"}}]}
        lines = ["data: " + json.dumps(fr), sse_finish("stop"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        assert any(isinstance(e, ReasoningDelta) and e.text == "думаю" for e in evs)

    def test_reasoning_content_alias_normalized_too(self):
        fr = {"choices": [{"index": 0,
                           "delta": {"reasoning_content": "hmm"}}]}
        lines = ["data: " + json.dumps(fr), sse_finish("stop"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        evs = list(c.stream_events(MSGS))
        assert isinstance(evs[0], ReasoningDelta) and evs[0].text == "hmm"

    def test_stream_simplification_drops_reasoning(self):
        """stream() (контракт Agent.run) отдаёт только текст + [DONE]."""
        fr = {"choices": [{"index": 0,
                           "delta": {"reasoning": "секретные мысли"}}]}
        lines = ["data: " + json.dumps(fr), sse("итог"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["итог", DONE_SENTINEL]


class TestBackwardCompatAlias:
    def test_old_names_alias_the_llamacpp_impl(self):
        """Эпоха «один класс + flavour»: старые имена = LlamaCppLLMClient."""
        from cascagent.client import OpenAIChatClient
        assert LlamaCppClientAlias is LlamaCppLLMClient
        assert OpenAIChatClient is LlamaCppLLMClient

    def test_old_import_shape_still_works(self):
        lines = [sse("a"), "data: [DONE]"]
        c = LlamaCppLLMClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["a", DONE_SENTINEL]


# ---------------------------------------------------------------------------
# StreamedResponse — объединённая сущность «стрим + адаптивный парсер»
# (решение 2026-10-14: thinking/tool_call приходят ЛИБО структурированными
# полями дельт, ЛИБО тегами прямо в content — конвейер поддерживает оба
# варианта одновременно без конфигурации)
# ---------------------------------------------------------------------------

def frame(delta: dict, finish_reason=None) -> str:
    obj = {"choices": [{"index": 0, "delta": delta}]}
    if finish_reason is not None:
        obj["choices"][0]["finish_reason"] = finish_reason
    return "data: " + json.dumps(obj, ensure_ascii=False)


def done() -> str:
    return "data: [DONE]"


def content_frames(text: str, *, split_tags: bool = False):
    """Сгенерировать SSE-фреймы content-дельт.

    По умолчанию режем текст по 7 символов: служебные теги (<think>,
    </think>) не разрываются по границам чанков, но слова и пробелы —
    да, так что проверка «тег распознан в потоке» остаётся осмысленной.
    ``split_tags=True`` — стресс-режим: принудительно ломаем каждый тег
    посередине, чтобы проверить PARTIAL-обработку незавершённого '<...'
    хвоста на границе фреймов.
    """
    if split_tags:
        # режим "рваных тегов": режем строго по 3 символа, как настоящий
        # стрим — теги приходят кусками через границу чанков
        return [frame({"content": text[i:i + 3]}) for i in range(0, len(text), 3)]
    return [frame({"content": text[i:i + 7]}) for i in range(0, len(text), 7)]


def kinds(state):
    """Порядок flat-ленты segments по kind (порядок рождения, не вложенности)."""
    return [s.kind.value for s in state.segments]


class TestStreamedResponse:
    def _client(self, lines):
        return LlamaCppLLMClient(LLMConfig(), session=FakeSession(lines))

    # ---- текстовый вариант: теги прямо в content ----------------------
    def test_tags_in_content_are_parsed(self):
        # модель пишет ... прямо внутри content —
        # ResponseParser распознаёт теги сам (whitelist + fuzzy BKTree),
        # куски тегов приходят через границу чанков
        text = "думал многоПривет мир"
        for split_tags in (False, True):   # обычный и «рваные теги» прогон
            lines = content_frames(text, split_tags=split_tags) + [done()]
            c = self._client(lines)
            sr = c.stream_parsed(MSGS)
            state = sr.run()
            assert kinds(state) == ["think", "response"], split_tags
            assert sr.parser.response_text() == "Привет мир", split_tags
            think_seg = next(s for s in state.segments if s.kind.value == "think")
            assert think_seg.finish_reason == "stop"  # закрыт ЯВНО тегом в потоке
            assert "думал" in think_seg.text, split_tags

    # ---- структурированный вариант: поле reasoning --------------------
    def test_structured_reasoning_becomes_think_segment(self):
        # llama.cpp шлёт thinking отдельными полями (reasoning /
        # reasoning_content), а content-дельты — это уже финальный ответ;
        # конвейер оборачивает структурные дельты в служебные теги парсера,
        # поэтому порядок сегментов = порядок рождения (think раньше response)
        lines = ([frame({"reasoning_content": "сначала "}),
                  frame({"reasoning": "подумаем"})]
                 + content_frames("ответ") + [done()])
        c = self._client(lines)
        sr = c.stream_parsed(MSGS)
        state = sr.run()
        assert kinds(state) == ["think", "response"]
        assert sr.parser.response_text() == "ответ"
        # текст живёт только в сегментах/дельтах; телеметрия — в StatsEvent
        think_seg = next(s for s in state.segments if s.kind.value == "think")
        assert "сначала" in think_seg.text and "подумаем" in think_seg.text
        assert think_seg.finish_reason == "stop"   # закрыт явно на FinishEvent
        assert sr.stats.reasoning_chars == len("сначала ") + len("подумаем")

    # ---- структурированный tool_call ----------------------------------
    def test_structured_tool_calls_become_tool_segment(self):
        lines = [frame({"tool_calls": [{"index": 0, "id": "c1",
                                        "function": {"name": "get_weather",
                                                     "arguments": ""}}]}),
                 frame({"tool_calls": [{"index": 0, "function":
                                        {"arguments": '{"city"'}}]}),
                 frame({"tool_calls": [{"index": 0, "function":
                                        {"arguments": ':"Москва"}'}}]}),
                 frame({}, finish_reason="tool_calls"), done()]
        c = self._client(lines)
        sr = c.stream_parsed(MSGS)
        state = sr.run()
        # контента не было → пустой RESPONSE-сегмент не создаётся вовсе
        assert kinds(state) == ["tool_call"]
        seg = state.segments[0]
        assert "get_weather" in seg.text and "Москва" in seg.text
        assert seg.finish_reason == "stop"       # закрыт явно на FinishEvent
        # сегмент скрытый (word.is_hidden наследует Segment.kind)
        assert all(w.is_hidden for w in seg.words)
        # протокольная агрегация tool_calls не потеряна
        assert sr.finish_event.tool_calls[0]["function"]["name"] == "get_weather"
        assert sr.finish_event.finish_reason == "tool_calls"
        assert sr.finish_reason == "tool_calls"  # продублирована на объекте

    # ---- смешанный случай: reasoning-поле И теги в content ------------
    def test_mixed_sources_single_think_segment(self):
        # сервер шлёт reasoning-поле ДО контента и при этом часть рассуждения
        # приходит тегами прямо в content — оба потока должны лечь в ОДИН
        # скрытый THINK-сегмент без наложения служебных тегов друг на друга
        # (конвейер никогда не открывает второй вложенный , пока
        # структурный сегмент ещё не закрыт — повторное открытие игнорируется)
        lines = ([frame({"reasoning_content": "раз "})]
                 + content_frames("думал дваПривет мир") + [done()])
        c = self._client(lines)
        sr = c.stream_parsed(MSGS)
        state = sr.run()
        # structural think закрыт только на FinishEvent → контентные дельты
        # фидятся ВНУТРЬ него; «Привет мир» тоже попадает в think (тег
        # </think> закрывает сегмент раньше, чем доходит до хвоста content)
        assert kinds(state) == ["think", "response"]
        assert sr.parser.response_text() == "Привет мир"
        think_seg = next(s for s in state.segments if s.kind.value == "think")
        assert "раз" in think_seg.text and "два" in think_seg.text
        assert think_seg.finish_reason == "stop"   # закрыт явно на FinishEvent

    # ---- аномальный finish: незакрытый структурный think --------------
    def test_abort_closes_open_segments_without_leaking_buffer(self):
        lines = [frame({"reasoning_content": "обрыв без хвоста"})]
        # сервер оборвался: ни finish_reason, ни [DONE] → мягкий режим "abort"
        c = self._client(lines)
        sr = c.stream_parsed(MSGS)
        state = sr.run()
        assert state.finish_reason == "abort"
        think = next(s for s in state.segments if s.kind.value == "think")
        assert think.kind.value == "think"
        assert think.finish_reason == "unclosed"

    # ---- голосование плагинов: вето останавливает коммит --------------
    def test_voter_veto_stops_commit(self):
        lines = [frame({"content": "стоп слово"}), done()]
        c = self._client(lines)
        seen_events = []

        def veto(item, st):
            seen_events.append(item)
            return False                      # первый же элемент — вето

        sr = c.stream_parsed(MSGS, voters=veto)
        state = sr.run()
        assert seen_events, "voter должен получать готовые элементы"
        # элемент отклонён → в ленту ничего не легло, непроглоченный хвост discarded
        assert len(state.words) == 0
        assert state.done is True

    # ---- ленивый интерфейс words() ------------------------------------
    def test_words_streams_incrementally(self):
        lines = [frame({"content": "привет мир"}), done()]
        c = self._client(lines)
        sr = c.stream_parsed(MSGS)
        got = [w.text for w in sr.words()]
        assert "".join(got).strip() == "привет мир"
        assert sr._consumed is True

    # ---- run() идемпотентен -------------------------------------------
    def test_run_is_idempotent(self):
        lines = [frame({"content": "раз два"}), done()]
        c = self._client(lines)
        sr = c.stream_parsed(MSGS)
        st1 = sr.run()
        n = len(st1.words)
        st2 = sr.run()
        assert len(st2.words) == n            # повторный прогон ничего не добавляет

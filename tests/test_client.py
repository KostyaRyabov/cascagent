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

from cascagent.client import DONE_SENTINEL, LlamaCppClient, LLMConfig


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
        c = LlamaCppClient(LLMConfig())
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
        c = LlamaCppClient(LLMConfig())
        p = c.build_payload(MSGS, use_think=False)
        assert p["messages"] == MSGS                    # llama_cpp: no rewrite
        assert MSGS[-1]["content"] == "Задача: собрать сад"

    def test_seed_and_stop_passthrough(self):
        c = LlamaCppClient(LLMConfig(seed=7, stop=["END"]))
        p = c.build_payload(MSGS)
        assert p["seed"] == 7
        assert p["stop"] == ["END"]

    def test_seed_zero_is_kept(self):
        """seed=0 disables server-side seeding but is still explicit."""
        c = LlamaCppClient(LLMConfig(seed=0))
        assert c.build_payload(MSGS)["seed"] == 0

    def test_no_seed_key_when_none(self):
        c = LlamaCppClient(LLMConfig())
        assert "seed" not in c.build_payload(MSGS)
        assert "stop" not in c.build_payload(MSGS)

    def test_per_call_overrides(self):
        c = LlamaCppClient(LLMConfig())
        p = c.build_payload(MSGS, temperature=0.9, max_tokens=64)
        assert p["temperature"] == pytest.approx(0.9)
        assert p["max_tokens"] == 64

    def test_llama_cpp_think_toggle_body_param(self):
        c = LlamaCppClient(LLMConfig(backend="llama_cpp"))
        assert c.build_payload(MSGS, use_think=False)["think"] is False

    def test_chatml_no_think_soft_prompt_on_last_user(self):
        c = LlamaCppClient(LLMConfig(backend="chatml"))
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
        c = LlamaCppClient(LLMConfig(backend="chatml"))
        p = c.build_payload(MSGS, use_think=True)
        assert p["messages"] == MSGS
        assert "think" not in p

    def test_unknown_backend_raises(self):
        c = LlamaCppClient(LLMConfig(backend="vllm"))
        with pytest.raises(ValueError):
            c.build_payload(MSGS)


class TestStream:
    def test_yields_deltas_then_done_sentinel(self):
        lines = [sse("Привет"), "", "keep-alive", sse(" мир"),
                 "data: [DONE]"]
        c = LlamaCppClient(session=FakeSession(lines))
        out = list(c.stream(MSGS))
        assert out == ["Привет", " мир", DONE_SENTINEL]

    def test_clean_eof_without_server_done_still_terminates(self):
        lines = [sse("a"), sse("b")]
        c = LlamaCppClient(session=FakeSession(lines))
        out = list(c.stream(MSGS))
        assert out[-1] == DONE_SENTINEL
        assert out[:-1] == ["a", "b"]

    def test_malformed_frame_skipped(self):
        lines = ["data: {not-json", sse("ok"), "data: [DONE]"]
        c = LlamaCppClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["ok", DONE_SENTINEL]

    def test_bytes_lines_decoded(self):
        lines = [sse("юни").encode("utf-8"), "data: [DONE]"]
        c = LlamaCppClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["юни", DONE_SENTINEL]

    def test_empty_delta_not_yielded(self):
        lines = ['data: {"choices":[{"index":0,"delta":{}}]}',
                 sse("x"), "data: [DONE]"]
        c = LlamaCppClient(session=FakeSession(lines))
        assert list(c.stream(MSGS)) == ["x", DONE_SENTINEL]

    def test_response_closed_even_mid_iteration(self):
        """The finally-close guard protects against leaking connections
        when the consumer breaks early (agent loop stops on plugin vote)."""
        lines = [sse("a"), sse("b"), "data: [DONE]"]
        sess = FakeSession(lines)
        c = LlamaCppClient(session=sess)
        it = c.stream(MSGS)
        assert next(it) == "a"
        it.close()                        # simulate early break
        assert sess.response.closed

    def test_http_error_propagates(self):
        sess = FakeSession([sse("a")], status_code=500)
        c = LlamaCppClient(session=sess)
        with pytest.raises(RuntimeError):
            list(c.stream(MSGS))
        assert sess.response.closed       # closed even on failure path

    def test_post_called_with_stream_and_timeout(self):
        sess = FakeSession(["data: [DONE]"])
        c = LlamaCppClient(LLMConfig(timeout=42.0), session=sess)
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
        c = LlamaCppClient(session=FakeSession(lines))
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
        c = LlamaCppClient(session=FakeSession(lines))
        collected = []
        for chunk in c.stream(MSGS):
            if chunk == DONE_SENTINEL:
                break
            collected.append(chunk)
        assert "".join(collected) == "Разбей задачу"

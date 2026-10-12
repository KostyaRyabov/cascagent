"""LLM transport client — OpenAI-compatible streaming (docs/stack.md §4–5).

Implements the transport contract fixed on 2026-10-13 (roadmap.md §9.4,
plugins.md §4.2): the LLM seam of ``Agent.run`` is a **chunk iterator** —
either a bare iterable or a ``callable(messages) -> iterator[str]``.
``LlamaCppClient.stream()`` is exactly such a callable: it yields raw text
deltas (SSE ``delta.content``), one chunk per server event, and ends with
the sentinel ``"[DONE]"`` line consumed by the agent loop. The client never
returns a plain string; think/final splitting is NOT its job either — the
built-in ``ResponseParser`` structures the stream (plugins.md §3.1).

Backend invariants honoured here (stack.md §4.3):
1. OpenAI-compatible endpoint (llama.cpp ``:8080`` default; ADR-001);
2. stable SYSTEM prefix comes from the caller (protocol §2) — the client
   sends messages verbatim;
3. think toggle at request level (§5.3): llama.cpp understands the
   ``"think": bool`` body parameter; TabbyAPI/chatml fallback uses the
   ``/no_think`` soft-prompt prepended to the LAST user message;
4. single-flight — one HTTP request per call, no batching, no warmup
   (AGENTS.md §9);
5. temperature default 0.2 (config.md §4 [llm]).

Transport is injectable (roadmap.md §10 Фаза 2 «transport инъектируем»):
the constructor takes a ``session`` object exposing ``post(url, json=...,
stream=..., timeout=...)`` — real usage passes ``requests.Session()``,
tests pass a fake (MockLLM feeds the agent directly, so this module has no
network-dependent tests).

CPU-thin boundary (roadmap.md §1 п.1): this module touches HTTP only; no
SQLite, no parsing heuristics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

#: Sentinel yielded after the last delta — matches the agent loop check
#: (memory.py run(): `if chunk == "[DONE]": break`).
DONE_SENTINEL = "[DONE]"


@dataclass
class LLMConfig:
    """[llm] section defaults (docs/config.md §4)."""

    url: str = "http://127.0.0.1:8080/v1/chat/completions"
    model: str = "local"
    temperature: float = 0.2
    max_tokens: int = 4000
    timeout: float = 300.0
    #: Request-level generation params passed through verbatim (seed for
    #: reproducibility — roadmap #4 §3.4; 0 disables server-side seeding).
    seed: Optional[int] = None
    stop: List[str] = field(default_factory=list)
    #: Backend flavour for the think mechanism (stack.md §5.3):
    #: "llama_cpp" → body param {"think": bool};
    #: "chatml"     → /no_think soft-prompt on the last user message.
    backend: str = "llama_cpp"


class LlamaCppClient:
    """Streaming chat client for llama.cpp server (OpenAI-compatible API).

    Usage as the agent's LLM seam::

        client = LlamaCppClient(LLMConfig())
        agent = Agent(name="decomposer", llm=lambda msgs: client.stream(msgs, use_think=True))

    or directly (``stream`` returns an iterator, the agent accepts callables
    of messages too when given the bound method + kwargs via partial).
    """

    def __init__(self, config: Optional[LLMConfig] = None,
                 session: Optional[Any] = None) -> None:
        self.config = config or LLMConfig()
        #: Injectable transport: anything with .post(url, json, stream,
        #: timeout) returning a response with .raise_for_status(),
        #: .iter_lines() and .close(). Default: a fresh requests.Session.
        if session is None:
            import requests          # lazy: keeps unit imports network-free
            session = requests.Session()
        self._session = session

    # ------------------------------------------------------------------
    def build_payload(self, messages: List[Dict[str, str]], *,
                      use_think: bool = True,
                      temperature: Optional[float] = None,
                      max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Compose the request body (pure function — testable offline).

        Think mechanics per stack.md §5.3:
        - llama.cpp: ``{"think": true|false}`` request parameter;
        - chatml fallback: prepend ``/no_think`` to the last user message
          when think is off (system prompt stays byte-exact — P8).
        """
        msgs = [dict(m) for m in messages]
        cfg = self.config
        payload: Dict[str, Any] = {
            "model": cfg.model,
            "messages": msgs,
            "temperature": cfg.temperature if temperature is None else temperature,
            "max_tokens": cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": True,
        }
        if cfg.seed is not None:
            payload["seed"] = cfg.seed
        if cfg.stop:
            payload["stop"] = list(cfg.stop)

        if cfg.backend == "llama_cpp":
            payload["think"] = bool(use_think)
        elif not use_think:
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user":
                    msgs[i]["content"] = "/no_think\n" + msgs[i].get("content", "")
                    break
        else:
            raise ValueError(f"unknown backend flavour: {cfg.backend!r}")
        return payload

    # ------------------------------------------------------------------
    def stream(self, messages: List[Dict[str, str]], *,
               use_think: bool = True,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> Iterator[str]:
        """Yield raw content deltas; finish with the ``[DONE]`` sentinel.

        SSE framing (OpenAI-compatible): lines ``data: {...}`` are parsed,
        ``delta.content`` extracted; blank lines and comments ignored. The
        server's own ``data: [DONE]`` is normalized into the same sentinel
        the agent loop expects.
        """
        payload = self.build_payload(messages, use_think=use_think,
                                     temperature=temperature,
                                     max_tokens=max_tokens)
        resp = self._session.post(
            self.config.url, json=payload, stream=True,
            timeout=self.config.timeout)
        try:
            resp.raise_for_status()
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if not raw.startswith("data:"):
                    continue                     # SSE comments / keep-alives
                data = raw[len("data:"):].strip()
                if data == "[DONE]":
                    yield DONE_SENTINEL
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue                       # tolerant: skip malformed frame
                choices = obj.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content
            yield DONE_SENTINEL                  # clean EOF without explicit DONE
        finally:
            close = getattr(resp, "close", None)
            if callable(close):
                close()

    # ------------------------------------------------------------------
    def as_callable(self, *, use_think: bool = True
                    ) -> Callable[[List[Dict[str, str]]], Iterator[str]]:
        """Adapter for ``Agent(llm=...)``: callable(messages) -> chunks."""
        def _call(messages: List[Dict[str, str]]) -> Iterator[str]:
            return self.stream(messages, use_think=use_think)
        return _call

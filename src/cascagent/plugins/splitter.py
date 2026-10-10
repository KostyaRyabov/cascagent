"""OutputSplitterPlugin — separates the raw completion into THINK / RESPONSE.

Standalone PostPlugin (leader decision 2026-10-11): think-block handling
is NOT part of TaskParserPlugin — it is a generic output-format concern,
so splitting lives in its own plugin and can be swapped when the model or
the output format changes. Runs BEFORE TaskParserPlugin in the post chain
(registered first), so downstream plugins receive an already-clean FINAL
RESPONSE via ``context["response"]``.

Markers: Qwen3 / llama.cpp chat-template tags ``...``;
the opening tag may carry attributes: ``<think attribute="...">``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

from cascagent.plugins.base import PostPlugin

# Think-block markers used by Qwen3 / llama.cpp chat templates.
THINK_OPEN_RE = re.compile(r"<\s*think\b[^>]*>", re.IGNORECASE)
THINK_CLOSE_RE = re.compile(r"<\s*/\s*think\s*>", re.IGNORECASE)

#: Local-memory keys owned by this plugin (per-run scope, wiped by agent.run()).
THINK_KEY = "think"
RESPONSE_KEY = "response"


class OutputSplitterPlugin(PostPlugin):
    """Cut the thinking block out of the raw streamed completion."""

    name = "OutputSplitterPlugin"

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------
    @staticmethod
    def _split_block(raw: str) -> Tuple[str, str]:
        """Core split: locate the think block, return (think, response).

        Cases handled (prompts.md §4.1):
        - complete block:      "<think>…</think>answer" → ("…", "answer");
        - unclosed block:      "<think>…" (model truncated) → everything
          after the open tag is think, response is whatever precedes it;
        - closed-only remnant: "…</think>answer" (opening tag lost) →
          content before the close tag was the think block;
        - no tags at all:      ("", raw).
        A second (hallucinated) think block inside the response area cuts
        the response short — response ends where the next block starts.
        Never raises.
        """
        if not raw:
            return "", ""

        m_open = THINK_OPEN_RE.search(raw)
        m_close = THINK_CLOSE_RE.search(raw)

        if m_open is None and m_close is None:
            return "", raw.strip()

        if (m_open is not None and m_close is not None
                and m_close.start() > m_open.end()):
            think = raw[m_open.end(): m_close.start()]
            response = raw[m_close.end():]
            extra = THINK_OPEN_RE.search(response)
            if extra:
                response = response[: extra.start()]
            return think.strip(), response.strip()

        if m_open is not None:
            # Unclosed think: tail is still thinking, no final answer yet.
            prefix = raw[: m_open.start()]
            think = raw[m_open.end():]
            return think.strip(), prefix.strip()

        # Only a closing tag present: content before it was the think block.
        think = raw[: m_close.start()]
        response = raw[m_close.end():]
        return think.strip(), response.strip()

    @classmethod
    def split_think_and_response(cls, raw: str) -> Tuple[str, str]:
        """Public API: split raw completion into (think, final_response).

        Thin wrapper over :meth:`_split_block` with the guarantee that both
        parts are stripped of leading/trailing whitespace. This is the ONLY
        atomicity/format knowledge downstream post-plugins should rely on —
        TaskParserPlugin gets the clean text via ``context["response"]``.
        """
        think, response = cls._split_block(raw or "")
        return think.strip(), response.strip()

    # ------------------------------------------------------------------
    # Plugin hook
    # ------------------------------------------------------------------
    def on_output(self, response: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Split the raw response; hand the clean text to the rest of POST.

        Writes ``think``/``response`` into the LOCAL memory (per-run scope —
        inter-plugin channel for this iteration only, never persisted) and
        updates ``context["response"]`` so the following post-plugins parse
        the FINAL RESPONSE without think noise.
        """
        think, final = self.split_think_and_response(response)
        self.local_memory.set(THINK_KEY, think)
        self.local_memory.set(RESPONSE_KEY, final)
        context[RESPONSE_KEY] = final
        return {"response": final,
                "parsed": {"think": think},
                "metadata": {"has_think": bool(think)},
                "action": "accept"}

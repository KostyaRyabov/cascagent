"""Base classes of the cascagent plugin architecture (v3).

Specification: docs/plugins.md §4 (sections), §6.3 (base classes), §12.2
(extension rules). Renaming applied on 2026-10-11 (leader decisions):

    old name        new name        rationale
    --------------  --------------  ------------------------------------
    InitPlugin      — REMOVED       one-time preparation is PrePlugin's job
                                    (lazy init guarded by a memory flag)
    InputPlugin     PrePlugin       runs BEFORE the LLM call; modifies ONLY
                                    user messages (system/assistant stay
                                    byte-exact — llama.cpp prefix cache, P8)
    TriggerPlugin   RuntimePlugin   runs DURING generation (per parsed
                                    word/special unit); should_stop
                                    REMOVED — stop semantics live in
                                    on_upload's operation types
    OutputPlugin    PostPlugin      runs AFTER generation (post-process)

Two structural decisions of 2026-10-11 shape the API below:

1. NO ``reset()`` anywhere. Keeping state clean between runs is NOT the
   plugin's job — it is the AGENT's: ``Agent.run()`` wipes the local
   namespace of its memory before every iteration. Plugins just never
   hold instance state themselves; transient data goes to memory.

2. Memories are BOUND ONCE at plugin registration (``bind()``), not
   passed through every hook call. Hot-path hooks run per TOKEN, so
   their signature stays minimal: ``on_token(token, accumulated)``.
   ALL FOUR hierarchy memories are bound: workspace / project / global /
   local (decision 2026-10-11) — any level is addressable from a hook as
   a plain attribute (e.g. task tree lives in ``self.project_memory``).

3. NO ``hook()`` marker on the root Plugin (removed by order): the ABC
   status comes from the section subclasses' abstract methods instead;
   the bare ``Plugin`` is a shared identity/config/bind container.

4. Naming convention: every concrete plugin class ends with the suffix
   ``Plugin`` (TaskParserPlugin, ...). Think/response splitting is NOT a
   plugin — OutputSplitterPlugin was removed (2026-10-12); stream
   structure is produced by the built-in agent service ``StreamParser``
   (cascagent.stream_parser, spec docs/stream_parser_model.md).

Contract notes:
- Plugins never call each other directly — communication is only through
  the shared local memory of one run (§12.2 п.2).
- The core data model is ``cascagent.models.Task`` / ``TaskStatus``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from cascagent.memory import Memory


class Plugin(ABC):
    """Common ancestor of every cascagent plugin.

    A plugin answers the question «how to process data at this phase of
    the loop», never «what should the agent do» (plugins.md §2.4).
    Stateless by contract: all data flows through the bound memories.
    """

    #: Unique registry name (PLUGIN_REGISTRY key, plugins.md §12.1).
    name: str = "base"

    def __init__(self, **config: Any) -> None:
        #: Free-form TOML config passed through by the agent builder
        #: (docs/config.md); stored verbatim for introspection/logging.
        self.config: Dict[str, Any] = dict(config)
        #: All four memories bound by Agent.add_plugin() once — see bind().
        #: Hooks address them as plain attributes; no memory arguments
        #: travel through hot-path calls.
        self.workspace_memory: Optional[Memory] = None
        self.project_memory: Optional[Memory] = None
        self.global_memory: Optional[Memory] = None
        self.local_memory: Optional[Memory] = None

    def bind(self, workspace_memory: Optional[Memory] = None,
             project_memory: Optional[Memory] = None,
             global_memory: Optional[Memory] = None,
             local_memory: Optional[Memory] = None) -> "Plugin":
        """Wire ALL hierarchy memories into this plugin (called once).

        Decision 2026-10-11: besides the agent's own two stores
        (``global_memory`` / ``local_memory``), the plugin also gets the
        ``project_memory`` and ``workspace_memory`` references, so any
        level can be addressed directly from a hook (e.g. TaskParserPlugin
        keeps the task tree in the PROJECT memory — tasks are a project
        entity). Upper levels may be absent (unwired agent) → stay None.
        """
        self.workspace_memory = workspace_memory
        self.project_memory = project_memory
        self.global_memory = global_memory
        self.local_memory = local_memory
        return self

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        cfg = ", ".join(f"{k}={v!r}" for k, v in sorted(self.config.items()))
        return f"<{type(self).__name__} {self.name}{' ' + cfg if cfg else ''}>"


class PrePlugin(Plugin):
    """Section PRE — runs before each LLM call (was InputPlugin).

    Absorbs the former InitPlugin role: heavy one-time preparation
    (indices, prompt pre-compilation, health checks) is done lazily on
    the first ``on_input`` invocation, guarded by a flag in
    ``self.global_memory`` (it must survive iterations, unlike local).

    ``messages`` is the FULL chat list ([{"role": ..., "content": ...},
    ...]) — the plugin SEES all of it, but by contract it may MODIFY
    ONLY ``user`` messages. ``system`` and ``assistant`` must be returned
    byte-identical: any change there breaks llama.cpp prefix caching
    (performance.md, P8), which is mandatory for us. Appending/removing
    whole user turns is allowed. Return the (possibly rebuilt) list.
    """

    name = "base_pre"

    @abstractmethod
    def on_input(self, messages: List[Dict[str, str]],
                 context: Dict[str, Any]) -> List[Dict[str, str]]:
        """Enrich/filter user messages of the outgoing request."""


class RuntimePlugin(Plugin):
    """Section RUNTIME — hot path, called on every parsed stream unit
    (WORD / SPECIAL) produced by the agent's built-in StreamParser
    (was TriggerPlugin). Budget: microseconds, NO I/O (§12.2 п.3).

    ``should_stop`` was removed: deciding when to cut the stream short
    is exactly what ``on_upload``'s boolean + finish_reason split is for.
    The hook signature changed from raw ``on_token(token, accumulated)``
    to ``on_upload(state, context)`` (decision 2026-10-12): plugins no
    longer see raw tokens — they vote on structured units in ParseState."""

    name = "base_runtime"

    def on_upload(self, state: "Any",
                  context: Dict[str, Any]) -> bool:
        """Vote on one ready element (WORD / SPECIAL) of the stream.

        ``state`` is the live ``cascagent.stream_parser.ParseState``
        passed BY REFERENCE (decision 2026-10-12, refined same day):
        plugins edit it in place — there is no returned state object.
        ``state.status`` tells which kind of element is pending
        (``WORD`` — closed word in ``state.word``; ``SPECIAL`` — a
        service tag from the whitelist, also in ``state.word``; PARTIAL
        elements are NOT delivered — plugins never see half-words).
        ``context`` is the run's shared context dict — passed BY
        REFERENCE too: all section hooks receive the same object;
        plugin-to-plugin data exchange goes through it.

        Return contract (decision 2026-10-12, final): ``bool`` —
        True = «all good, keep the loop running»; False = stop the
        stream NOW. On the FIRST False the agent breaks the loop and
        inspects ``state.finish_reason`` to decide what to do:
        - ``"stop"``   → natural end without error (EOS, budget met);
        - ``"error"``  → corrupted output / guard tripped; the agent
          raises RuntimeError. For a custom message set
          ``state.error = "..."`` alongside finish_reason — it is
          carried into the exception text and the run result.
        Leaving ``state.finish_reason`` untouched while returning
        False is treated as ``"error"`` (a stop without a declared
        reason is suspicious by definition).

        So the idiomatic body is: mutate ``state`` (counters,
        finish_reason/error) as needed, then ``return True``.
        A bare ``Plugin`` subclass carries no stream opinion at all →
        pass-through default ``True``.

        Commit rule (docs/stream_parser_model.md, §7): the element is
        applied to the segment tree (word appended to ``stack[-1].text``;
        special opens/closes a Segment) ONLY if EVERY RuntimePlugin
        returned True. Position info for budget guards:
        ``state.token_offset`` (ordinal number of the current element)
        and ``state.word_count`` (committed words so far). Transient
        buffers must live in ``self.local_memory`` — the agent wipes it
        next run(), so nothing here needs manual cleanup.
        """
        return True


class PostPlugin(Plugin):
    """Section POST — runs after generation completes (was OutputPlugin).

    Parses and validates the final response, may trigger a retry of the
    WHOLE run (bounded by ``max_retries`` in the agent config). Idempotent
    by contract (§12.2 п.4): re-running on the same response yields the
    same result — safe because intermediate stage data lives in the local
    namespace, wiped at every run().
    """

    name = "base_post"

    @abstractmethod
    def on_output(self, response: str,
                  context: Dict[str, Any]) -> Dict[str, Any]:
        """Process the final response.

        Result API (plugins.md §4.4):
        ``{"response": str, "parsed": Any, "metadata": dict,
           "action": "accept"|"retry"|"reject"}``.
        """

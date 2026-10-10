"""TaskParserPlugin — the decomposition post-processing plugin (PostPlugin).

ALL parsing / duplicate-detection logic lives in this class as static
methods (leader decisions 2026-10-11): the former module ``cascagent.parser``
and ``detector.py`` are deleted — duplicate / parent-echo detection is
TaskParserPlugin's job now (rules F2/§3.7, process.md §1.3). Think/response
splitting moved OUT to ``OutputSplitterPlugin`` (plugins/splitter.py) — a
generic output-format concern; by the time this plugin runs, the response
is already clean. Sanitizing (sanitize_line/sanitize_text) and lexical
normalization (normalize) are REMOVED by direct order: no junk-stripping,
no comparison canon — duplicates are caught on raw strings only.

Specification: docs/protocol.md §2–3 (format + system prompt), docs/process.md
§1.3 (three-stage pipeline), AGENTS.md §4/§8.

Responsibilities:
- ``SYSTEM_PROMPT`` — the fixed system prompt of the decomposer (module
  constant → stable prefix for llama.cpp KV cache, P8);
- ``parse_decomposition`` — stage 1: blocks «Название / Описание» separated
  by an empty line → candidate tasks, two-level ``<atom>`` (A3/A4);
- ``similarity`` / ``is_parent_repeat`` / ``is_duplicate`` — duplicate
  detection on RAW strings (the absorbed DuplicateDetector logic);
- ``is_atomic`` — plain ``ATOM_MARKER in response`` check;
- ``build_user_prompt`` — pure-function USER prompt constructor (P6);
- ``on_output`` — stages 2 (filtering F1–F3) and 3 (attaching the subtree
  to the parent in the PROJECT memory key ``tasks`` — tasks are a project
  entity, so this plugin reads/writes ``self.project_memory``, bound at
  add_plugin() time).

No intermediate states are persisted: raw stage-1 candidates live only in
local variables; memory receives the final task tree exclusively.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from cascagent.models import Task
from cascagent.plugins.base import PostPlugin

# --------------------------------------------------------------------------
# System prompt of the decomposer — FIXED VERBATIM (protocol.md §2).
# Changing the text requires updating the snapshot test together with it
# (AGENTS.md §4: менять можно только вместе с тестами).
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """\
Разбей задачу на подзадачи следующего уровня детализации.

ФОРМАТ ОТВЕТА:
1. Название задачи одной строкой
2. Описание что нужно сделать

Между подзадачами — ПУСТАЯ СТРОКА (разделитель).

ПРАВИЛА:
- Подзадачи должны полностью покрывать исходную задачу
- Каждая подзадача — конкретное действие
- Не повторяй исходную задачу
- Если подзадача уже атомарна (одно простое действие) — в описании напиши только: <atom>
- Если ВСЯ исходная задача не делится — верни только одну строку: <atom>\
"""

#: Atomic marker — the ONLY atomicity signal from the model (no heuristics,
#: AGENTS.md §8.1). Two levels: response contains <atom> (global atom),
#: second line of a block == <atom> (atomic subtask).
ATOM_MARKER = "<atom>"

#: Default similarity threshold for duplicate/parent-echo checks
#: (config [decomposer] similarity_threshold, data.md §1 F2).
DEFAULT_THRESHOLD = 0.75

#: Memory keys owned by this plugin (naming conventions — plugins.md §5.7).
TASKS_KEY = "tasks"            # PROJECT level: {task.id: Task} tree
PARSE_ERROR_KEY = "parse_error"

# Empty line = subtask separator (protocol §3 п.2).
_BLOCK_SPLIT_RE = re.compile(r"\n[ \t]*\n")


class TaskParserPlugin(PostPlugin):
    """Parse, validate and attach one decompose answer (protocol v2).

    Every primitive below is a ``@staticmethod`` — the class is the single
    namespace for the whole CPU-side decomposition logic; no external
    parser module exists anymore.
    """

    name = "TaskParserPlugin"

    def __init__(self, threshold: float = DEFAULT_THRESHOLD,
                 dedup_siblings: bool = True) -> None:
        super().__init__(threshold=threshold, dedup_siblings=dedup_siblings)
        #: Similarity threshold for F2 / pairwise dedup (config [decomposer]).
        self.threshold = threshold
        #: Remove near-identical children within one answer (F2 pairwise).
        self.dedup_siblings = dedup_siblings

    # ------------------------------------------------------------------
    # Primitives: duplicate detection (absorbed DuplicateDetector logic)
    # Comparison runs on RAW strings — normalize() was removed by order.
    # ------------------------------------------------------------------
    @staticmethod
    def similarity(a: str, b: str) -> float:
        """SequenceMatcher ratio on the given strings, [0..1] (A5/A6)."""
        a, b = (a or ""), (b or "")
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        return SequenceMatcher(None, a, b).ratio()

    @staticmethod
    def is_parent_repeat(child_brief: str, parent_brief: str,
                         threshold: float = DEFAULT_THRESHOLD) -> bool:
        """Degenerate-repeat rule (protocol §3.7): child brief ≈ parent brief."""
        return TaskParserPlugin.similarity(child_brief, parent_brief) >= threshold

    @staticmethod
    def is_duplicate(new_brief: str, existing: List[str],
                     threshold: float = DEFAULT_THRESHOLD) -> bool:
        """True if ``new_brief`` is >= threshold similar to any existing brief."""
        return any(TaskParserPlugin.similarity(new_brief, e) >= threshold
                   for e in existing)

    @staticmethod
    def is_atomic(response: str) -> bool:
        """Global atomicity: the plain marker check (decision 2026-10-11) —
        just ``ATOM_MARKER in response``; nothing else (AGENTS.md §8.1: NO
        heuristics)."""
        return ATOM_MARKER in (response or "")

    # ------------------------------------------------------------------
    # Primitive: stage 1 — parse (never invents, never truncates, §8.2)
    # Lines are taken AS-IS: sanitize_line/sanitize_text are gone by order.
    # ------------------------------------------------------------------
    @staticmethod
    def parse_decomposition(final_response: str) -> List[Dict[str, object]]:
        """Stage 1 of the pipeline: raw FINAL RESPONSE → candidates.

        Grammar (protocol §3, эталонный листинг):
        - response containing ``<atom>`` → global atom → empty list (no
          children; the caller executes the task itself);
        - empty response → [] (caller treats as atomic via :meth:`is_atomic`);
        - every non-empty block between empty lines = one subtask: first line =
          ``brief``, remaining lines = ``description`` joined by single spaces;
        - ``<atom>`` as the only description line → atomic subtask
          (``description=None``, ``is_atom=True``);
        - backward compat: legacy indented format — a block whose continuation
          lines keep leading whitespace is accepted too;
        - never invents tasks, never truncates the list (AGENTS.md §8.2 —
          filtering happens on stage 2).

        Returns dicts ``{"brief": str, "description": str|None, "is_atom": bool}``
        (data.md §3 ``parsed`` field contract). ``on_output`` turns each
        candidate into a ``Task`` — all remaining fields (snowflake id,
        embedding(brief), depth) are formed in ``Task.__init__`` in one step.
        """
        text = (final_response or "").strip()
        if not text:
            return []
        if TaskParserPlugin.is_atomic(text):
            return []  # global atom: the task is not decomposed

        blocks = TaskParserPlugin._split_blocks(text)
        if not blocks:
            return []
        # Preamble (protocol §3 п.6): the first block is the whole answer only
        # when it is a single bare line that is neither <atom> nor a two-line
        # «Название + описание» task — then it is model chatter and is dropped.
        first = blocks[0]
        if len(blocks) > 1 and len(first) == 1 and len(first[0]) > 1:
            blocks = blocks[1:]

        result: List[Dict[str, object]] = []
        for lines in blocks:
            brief = lines[0][0]
            rest = [l for l, _ in lines[1:]]
            if not brief:
                continue  # blank line — nothing actionable
            if len(rest) == 1 and rest[0].lower().rstrip(".:") == ATOM_MARKER:
                result.append({"brief": brief, "description": None,
                               "is_atom": True})
            else:
                description = " ".join(rest) or None
                result.append({"brief": brief, "description": description,
                               "is_atom": False})
        return result

    @staticmethod
    def _split_blocks(text: str) -> List[List[Tuple[str, bool]]]:
        """Split the response into subtask blocks (protocol §3 п.2/п.4).

        Primary grammar: empty line = separator. Backward compat (п.4): with
        no empty lines at all, a new block starts at each non-indented line;
        indented lines are description continuations of the current block.
        Each block = list of (raw_line, had_leading_whitespace) — lines are
        kept byte-as-is (no sanitizing, decision 2026-10-11).
        """
        raw_blocks = [b for b in _BLOCK_SPLIT_RE.split(text) if b.strip()]
        if len(raw_blocks) <= 1 and any(
                r[:1].isspace() for r in text.splitlines() if r.strip()):
            # Legacy indented format: regroup by leading-whitespace boundaries.
            grouped: List[List[str]] = []
            for raw in text.splitlines():
                if not raw.strip():
                    continue
                if raw[:1].isspace() and grouped:
                    grouped[-1].append(raw)
                else:
                    grouped.append([raw])
            raw_blocks = ["\n".join(g) for g in grouped]

        blocks: List[List[Tuple[str, bool]]] = []
        for chunk in raw_blocks:
            lines: List[Tuple[str, bool]] = []
            for raw in chunk.split("\n"):
                if not raw.strip():
                    continue
                s = raw.strip()
                if s.lower().rstrip(".:") == ATOM_MARKER and lines:
                    s = ATOM_MARKER  # accept wrapped forms like `<atom>.`
                lines.append((s, raw[:1].isspace()))
            if lines:
                blocks.append(lines)
        return blocks

    # ------------------------------------------------------------------
    # Primitive: user-prompt construction (P6, used by the orchestrator)
    # ------------------------------------------------------------------
    @staticmethod
    def build_user_prompt(brief: str,
                          context_lines: Optional[List[str]] = None,
                          completed_siblings: Optional[List[Tuple[str, str]]] = None
                          ) -> str:
        """Compose the minimal USER prompt for one isolated decomposition call.

        Fixed-form pure function (P6): stable shape keeps llama.cpp prefix
        caching effective; the model sees only its task brief + enriched
        context assembled by the CPU layer (protocol §5). It does NOT know
        about RAG/semantic memory sources.

        Args:
            brief: task.brief of the node being decomposed.
            context_lines: ready-made lines of the «Контекст» block (enricher
                output; parent title appears here only if the enricher added it).
            completed_siblings: (brief, result_summary) pairs already done.
        """
        parts: List[str] = [f"Задача: {brief}"]

        ctx = [c for c in (context_lines or []) if c and c.strip()]
        if ctx:
            parts.append("Контекст:\n" + "\n".join(ctx))

        if completed_siblings:
            lines = [f"- {b} ✓ {r[:100]}" if r else f"- {b} ✓"
                     for b, r in completed_siblings]
            parts.append("Выполненные ранее:\n" + "\n".join(lines))

        parts.append("РАЗБЕЙ НА ПОДЗАДАЧИ:")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Plugin hook: stages 2 (filter) + 3 (attach) over one final response
    # ------------------------------------------------------------------
    def on_output(self, response: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Full post-processing pass over one FINAL RESPONSE.

        ``context`` contract (set by the orchestrator before the run):
        - ``task`` (Task, REQUIRED) — the node being decomposed; its brief
          drives the parent-echo rule (F2/§3.7);
        - ``response`` (str, optional) — set by OutputSplitterPlugin to the
          think-free text; takes precedence over the raw argument.
        - ``think`` (str, optional) — think block, only logged downstream.

        Tasks are a PROJECT entity: the tree is read from and written to
        ``self.project_memory`` under the key ``tasks`` (survives run()
        iterations; the agent wipes only the local namespace). No
        intermediate state is written to memory anywhere.
        """
        clean = context.get("response", response)
        task: Optional[Task] = context.get("task")
        if task is None:  # critical input missing — visible error, not swallowed
            self.local_memory.set(PARSE_ERROR_KEY, "context['task'] is required")
            return {"response": clean, "parsed": [],
                    "metadata": {"error": "missing_context_task"},
                    "action": "reject"}

        # -- stage 1: parse (candidates live only in this local variable)
        candidates = TaskParserPlugin.parse_decomposition(clean)

        # -- stage 2: deterministic filtering F1–F3 + pairwise dedup
        kept: List[Dict[str, Any]] = []
        removed_dupes = 0
        for cand in candidates:
            brief = cand["brief"]
            # F1: brief == description ⇒ the "description" adds nothing — atom.
            if (not cand["is_atom"] and cand["description"] is not None
                    and cand["description"].strip() == brief.strip()):
                cand = {"brief": brief, "description": None, "is_atom": True}
            # F2 (protocol §3.7): child echoes the parent ⇒ degenerate repeat.
            if TaskParserPlugin.is_parent_repeat(brief, task.brief, self.threshold):
                removed_dupes += 1
                continue
            # Pairwise dedup among already-kept children.
            if (self.dedup_siblings
                    and TaskParserPlugin.is_duplicate(
                        brief, [k["brief"] for k in kept], self.threshold)):
                removed_dupes += 1
                continue
            kept.append(cand)

        is_global_atom = TaskParserPlugin.is_atomic(clean) or not kept
        # F3: nothing survived filtering (or model said <atom>) ⇒ parent is atom.
        task.is_atom = is_global_atom

        # -- stage 3: build Task subtree and attach it to the parent in `tasks`
        children: List[Task] = []
        if not is_global_atom:
            for cand in kept:
                children.append(Task(brief=cand["brief"],
                                     description=cand["description"],
                                     is_atom=cand["is_atom"],
                                     parent=task))
        task.subtasks = children  # replace, idempotent on re-run

        store = self.project_memory
        if store is None:
            self.local_memory.set(
                PARSE_ERROR_KEY, "project memory is not wired (Agent.project)")
            return {"response": clean, "parsed": children,
                    "metadata": {"error": "missing_project_memory"},
                    "action": "reject"}

        tasks_tree: Dict[Any, Task] = dict(store.get(TASKS_KEY, {}))
        # Drop the previous subtree of this parent first — a re-run (retry)
        # replaces children, never accumulates stale Task objects.
        for stale in [t for t in tasks_tree.values() if t.parent is task]:
            del tasks_tree[stale.id]
        tasks_tree[task.id] = task
        for child in children:
            tasks_tree[child.id] = child
        store.set(TASKS_KEY, tasks_tree)

        metadata = {
            "is_atomic": is_global_atom,
            "children": len(children),
            "duplicates_removed": removed_dupes,   # data.md §3 call record
        }
        return {"response": clean,
                "parsed": ([] if is_global_atom else children),
                "metadata": metadata,
                "action": "accept"}

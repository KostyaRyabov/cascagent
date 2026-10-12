"""ResponseParser — built-in agent service that structures an LLM stream.

Renamed from ``StreamParser`` (leader decision 2026-10-13: there is only ONE
parser in the project and it is called ResponseParser). NOT a plugin
(leader decision 2026-10-12; OutputSplitterPlugin was removed
in favour of this service). Specification: docs/stream_parser_model.md
(final model v3 — flat ``segments`` list + open ``stack``, no ``Unit``,
no tree ``root``; every Segment has an explicit kind, RESPONSE == «outside
of tags»).

Responsibility: consume raw streamed chunks (llama.cpp SSE ``delta.content``
or plain token strings), split them into words / service tags and maintain
the single live ``ParseState``. Runtime plugins vote on each ready element
via ``RuntimePlugin.on_upload(state, context) -> bool`` (decision 2026-10-12:
state/context are passed by reference — the hook edits state in place and
returns True to keep the loop running / False to stop it; the agent then
inspects ``state.finish_reason`` ("stop" = clean end, "error"/unset = raise,
custom text in ``state.error``)); an element is applied to the segment tree
ONLY when every plugin returned True.

Fixed rules (docs/stream_parser_model.md §«Правила»):
1. Punctuation is not a unit type — it only closes the current word.
2. Segments = tag blocks; RESPONSE is the implicit «outside tags» segment.
3. Nesting allowed (whitelist + depth limit); tolerant closing: a stray
   closing tag is noise (appended as text), an explicit close of an outer
   tag silently closes inner ancestors (finish_reason="unclosed").
4. tool_call inside think = anomaly: think is force-closed with
   finish_reason="aborted", payload extracted, the aborted think is kept
   for debugging; the agent ends the iteration and starts a new run().
5. Self-closing tags (<br/>) do not move the stack.
6. Tag typos are recognised fuzzily: BKTree + Levenshtein over whitelist.
7. A segment is born / dies by the same plugin vote as a word.
8. On [DONE]/finish_reason: buffer flushes as a word, every open segment
   closes with finish_reason="unclosed".
9. max_buffer_chunks (parser config, NOT state): overflow flushes the
   buffer as a WORD.

Unknown tags (not in whitelist) are treated as ordinary text: '<' stays
in the buffer until a word boundary or '>' resolves it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Characters that close a word (rule 1: punctuation is not a unit type).
WORD_BOUNDARY_RE = re.compile(r"[\s\u00a0.,;:!?\n\r\t]+")
#: A complete tag candidate: <name ...> or </name> (attrs tolerated).
TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z_][\w\-]*)((?:[^<>]|\"[^\"]*\")*)>")
#: Trailing slash inside the attribute area → self-closing (rule 5).
_SELF_CLOSING_RE = re.compile(r"/\s*$")


class ParseStatus(str, Enum):
    """How far the tokens of the current iteration formed an element.

    Element-status enum. The parser service itself is ``ResponseParser``
    (renamed from StreamParser, leader decision 2026-10-13); the live
    mutable state object passed to RuntimePlugins stays ``ParseState``.
    """

    PARTIAL = "partial"  # element unfinished — wait for more chunks
    WORD = "word"        # word explicitly closed (space/punct/message end)
    SPECIAL = "special"  # service tag from the whitelist recognized


class SegmentKind(str, Enum):
    THINK = "think"
    RESPONSE = "response"  # de facto — the «outside of tags» state
    TOOL_CALL = "tool_call"
    # ... extended by the parser whitelist (config)


@dataclass
class Word:
    """One committed element of the editable segment tape (model v5,
    implemented 2026-10-13; decisions A–F in docs/stream_parser_model.md).

    Lifecycle rule: the parser creates a Word ONLY at commit time (the
    WORD/SPECIAL event passed every RuntimePlugin vote) — append-at-commit,
    mutate-after. Plugins may then edit ``text``, reorder/remove/add words
    through direct access to ``segment.words`` (leader decision 2026-10-13:
    words and segments are the ONLY things you manage — no flat-text
    editing API).
    """

    text: str = ""                # word text INCLUDING its closing delimiter
                                  # (decision C: no separate tail field)
    segment: "Segment" = None     # direct link to the owning segment
                                  # (decision A: links, not indices)
    idx: int = 0                  # GLOBAL ordinal from the start of the
                                  # response generation (decision F)
    is_hidden: bool = False       # inherited from the segment on creation
                                  # (decision E: e.g. THINK → hidden);
                                  # per-word override allowed afterwards

    def __post_init__(self) -> None:
        # Any mutation of the words list invalidates the segment's lazy
        # text cache (decision B).
        if self.segment is not None:
            self.segment._text_cache = None


@dataclass
class Segment:
    """One output block. ``words`` is the primary representation (v5):
    ONLY confirmed elements land here (every RuntimePlugin voted continue).
    ``text`` stays a backwards-compatible derived view (lazy, cached until
    the words list is touched)."""

    kind: SegmentKind
    words: List[Word] = field(default_factory=list)
    finish_reason: Optional[str] = None   # None = open; stop|length|aborted|unclosed
    segments: List["Segment"] = field(default_factory=list)  # subsegments
    #: Lazy cache for the derived ``text`` (decision B); invalidated by
    #: Word.__post_init__ and by any structural change of ``words``.
    _text_cache: Optional[str] = None

    @property
    def is_hidden(self) -> bool:
        """Decision E: visibility is bound to SegmentKind — THINK and
        TOOL_CALL segments are hidden from the user stream; RESPONSE is
        visible. Words inherit this value at commit time."""
        return self.kind in (SegmentKind.THINK, SegmentKind.TOOL_CALL)

    @property
    def text(self) -> str:
        """Derived: ''.join(w.text for w in words), lazily cached (B)."""
        if self._text_cache is None:
            self._text_cache = "".join(w.text for w in self.words)
        return self._text_cache

    @text.setter
    def text(self, value: str) -> None:
        """Back-compat setter (old code/tests wrote raw text into a
        segment): replaces the whole word list with a single Word.
        ``idx`` stays untouched — global renumbering is owned by the
        parser's tape rebuild (ParseState.words)."""
        self.words = [Word(text=value, segment=self, is_hidden=self.is_hidden)]

    # --- direct word management (leader decision 2026-10-13: manage
    #     words/segments directly; these helpers keep idx consistent) ---
    def add_word(self, word: "Word") -> "Word":
        """Append an existing/new Word to this segment (fixes the link;
        global ``idx`` is owned by the parser tape)."""
        word.segment = self
        self.words.append(word)
        self._text_cache = None
        return word

    def remove_word(self, word: "Word") -> bool:
        """Remove a Word object from this segment."""
        try:
            self.words.remove(word)
        except ValueError:
            return False
        self._text_cache = None
        return True

    def move_word(self, word: "Word", to_pos: int) -> None:
        """Reorder: move an existing Word to position ``to_pos``."""
        self.words.remove(word)
        self.words.insert(max(0, min(to_pos, len(self.words))), word)
        self._text_cache = None

    def reindex(self) -> None:
        """Invalidate the text cache after any external mutation of
        ``words`` (reorder/remove/insert done through the raw list)."""
        self._text_cache = None


@dataclass
class ParseState:
    """The single CURRENT state of the parser (model v3)."""

    # --- status of the current element ---
    status: ParseStatus = ParseStatus.PARTIAL
    word: Optional[str] = None            # filled only for WORD / SPECIAL

    # --- word reconstruction ---
    buffer: str = ""                      # raw un-assembled tail

    # --- stream position (decision 2026-10-12; two counters per leader
    #     decision 2026-10-13: tokens AND words, global over the response) ---
    token_offset: int = 0                 # ordinal number of the current
                                          # element delivered to plugins
    token_count: int = 0                  # raw chunks fed (transport units)
    word_count: int = 0                   # committed WORDs so far

    # --- segments ---
    segments: List[Segment] = field(default_factory=list)  # all collected
    stack: List[Segment] = field(default_factory=list)     # OPEN ones; [-1] = current

    #: Global word tape (decision F): every committed Word in generation
    #: order, across all segments. ``word.idx == len(words)`` at commit →
    #: idx is the position in this list AND equals the word_count counter.
    words: List["Word"] = field(default_factory=list)

    # --- stream / protocol ---
    finish_reason: Optional[str] = None   # stop | length | tool_calls | ...
    error: Optional[str] = None           # custom message when finish_reason == "error"
    done: bool = False                    # [DONE] received

    @property
    def current(self) -> Optional[Segment]:
        """Innermost open segment (None before the first RESPONSE is born)."""
        return self.stack[-1] if self.stack else None


# ----------------------------------------------------------------------
# Fuzzy tag dictionary: BKTree + Levenshtein (rule 6)
# ----------------------------------------------------------------------

def _levenshtein(a: str, b: str) -> int:
    """Classic DP edit distance (small strings: tag names only)."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[len(b)]


class BKTree:
    """Small BK-tree over Levenshtein for fuzzy tag-name lookup."""

    def __init__(self, words: List[str]) -> None:
        self._words = list(words)
        self._root: Optional[Dict[Any, Any]] = None
        for w in self._words:
            self._insert(w)

    def _insert(self, word: str) -> None:
        if self._root is None:
            self._root = {"word": word, "children": {}}
            return
        node = self._root
        while True:
            d = _levenshtein(word, node["word"])
            if d == 0:
                return
            kids = node["children"]
            if d not in kids:
                kids[d] = {"word": word, "children": {}}
                return
            node = kids[d]

    def find(self, query: str, max_dist: int) -> List[str]:
        """All stored words within ``max_dist`` edits of ``query``."""
        if self._root is None or max_dist < 0:
            return []
        out: List[str] = []
        stack = [self._root]
        while stack:
            node = stack.pop()
            d = _levenshtein(query, node["word"])
            if d <= max_dist:
                out.append(node["word"])
            for dist, child in node["children"].items():
                if d - max_dist <= dist <= d + max_dist:
                    stack.append(child)
        return out


class ResponseParser:
    """Chunk-by-chunk structuring service owned by the Agent.

    Usage (the agent drives this loop inside run()):

        parser = ResponseParser(whitelist=["think", "tool_call"], ...)
        for chunk in llm.stream(messages):
            for ev in parser.feed(chunk):        # [(WORD|SPECIAL, payload)]
                if not all plugins vote continue:
                    break                         # element NOT committed
                parser.commit(ev)
        parser.finalize("stop")                   # or "length" / "[DONE]"

    The parser never calls plugins itself — voting is orchestrated by
    the agent (plugins.md §12.2: components talk through the agent/state).
    """

    def __init__(self,
                 whitelist: Tuple[str, ...] = ("think", "tool_call"),
                 max_depth: int = 4,
                 fuzzy_max_distance: int = 1,
                 max_buffer_chunks: int = 256,
                 on_anomaly: Optional[Callable[[Segment], None]] = None,
                 ) -> None:
        #: Known service tags (config, not state — rule 4 of model doc).
        self.whitelist = tuple(w.lower() for w in whitelist)
        #: Tag name → SegmentKind registry (extensible via add_kind()).
        self.tag_kinds: Dict[str, SegmentKind] = {
            "think": SegmentKind.THINK,
            "tool_call": SegmentKind.TOOL_CALL,
        }
        self.max_depth = max_depth
        self.fuzzy_max_distance = fuzzy_max_distance
        self.max_buffer_chunks = max_buffer_chunks
        #: Callback the agent may hook to react to rule-4 anomalies live.
        self.on_anomaly = on_anomaly
        #: Rule-4 payload: tool_call segment extracted from inside think.
        self.tool_call_payload: Optional[Segment] = None
        self._bktree = BKTree(list(self.whitelist))
        self.state = ParseState()
        self._chunks_in_buffer = 0

    def add_kind(self, tag: str, kind: SegmentKind) -> None:
        """Extend whitelist + kind registry (config-time, before feeding)."""
        tag = tag.lower()
        if tag not in self.whitelist:
            self.whitelist = self.whitelist + (tag,)
            self._bktree = BKTree(list(self.whitelist))
        self.tag_kinds[tag] = kind

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def feed(self, chunk: str) -> List[Tuple[ParseStatus, Optional[str]]]:
        """Consume one raw SSE delta / token string.

        Returns the READY events produced by this chunk — empty list while
        the element stays PARTIAL (plugins must not see half-words).
        Each event is ``(status, payload)``: WORD → the closed word,
        SPECIAL → normalized tag body ("think", "/think", "br/").
        """
        st = self.state
        if st.done:
            raise RuntimeError("stream already finalized ([DONE] received)")
        st.token_count += 1               # raw transport counter (chunks)
        st.buffer += chunk
        self._chunks_in_buffer += 1
        events: List[Tuple[ParseStatus, Optional[str]]] = []
        while True:
            ev = self._next_event()
            if ev is None:
                break
            # ordinal position of the element being delivered to plugins
            st.token_offset += 1
            events.append(ev)
        # rule 9: buffer overflow → flush the remainder as one WORD
        if (self._chunks_in_buffer >= self.max_buffer_chunks
                and st.buffer.strip()):
            st.token_offset += 1
            events.append(self._flush_buffer_as_word())
        return events

    def commit(self, event: Tuple[ParseStatus, Optional[str]]) -> None:
        """Apply a VOTED element to the segment tree (rule 7).

        Call ONLY after every RuntimePlugin.on_upload returned continue.
        """
        status, payload = event
        st = self.state
        if status is ParseStatus.WORD:
            assert payload is not None
            target = st.current or self._ensure_response_segment()
            # v5 Word model: append-at-commit. text carries the closing
            # delimiter (decision C); idx is GLOBAL over the response tape
            # (decision F) — equal to word_count before increment; the Word
            # links back to its segment (decision A) and inherits visibility
            # from Segment.kind (decision E).
            word = Word(text=payload + " ", segment=target,
                        idx=st.word_count, is_hidden=target.is_hidden)
            target.words.append(word)
            target._text_cache = None
            st.words.append(word)          # global tape (idx == position)
            st.word_count += 1             # committed words counter
        elif status is ParseStatus.SPECIAL:
            assert payload is not None
            self._apply_special(payload)
        else:                                 # PARTIAL is never committed
            raise ValueError(f"cannot commit a partial element: {event!r}")

    def drain_buffer(self) -> Optional[Tuple[ParseStatus, Optional[str]]]:
        """Turn the raw buffer remainder into one votable WORD event.

        Shared by rule 9 (max_buffer_chunks overflow), the agent's
        post-stream tail flush and finalize(). Clears the buffer and the
        chunk counter, advances ``token_offset`` (the element is being
        DELIVERED to plugins — they decide via vote whether commit()
        applies it to the segment tree). Returns None when the buffer is
        empty or already consumed (rule 7: PARTIAL is never an event).
        """
        st = self.state
        leftover = st.buffer.strip()
        if not leftover:
            st.buffer = ""
            self._chunks_in_buffer = 0
            return None
        st.buffer = ""
        self._chunks_in_buffer = 0
        st.token_offset += 1
        return (ParseStatus.WORD, leftover)

    def finalize(self, reason: str = "stop") -> ParseState:
        """Close the stream ([DONE] / finish_reason). Rule 8.

        Buffer remainder flushes as a word into the current segment;
        every still-open segment closes with finish_reason="unclosed";
        the implicit RESPONSE segment closes with the protocol ``reason``.
        """
        st = self.state
        if not st.done:
            tail = self.drain_buffer()
            if tail is not None:
                # Rule-8 tail flush IS a committed word: deliver it through
                # the same WORD/SPECIAL pipeline (token_offset counter) so
                # the last word of the stream still gets its runtime vote.
                self.commit(tail)
            st.status = ParseStatus.WORD
            st.word = None
            while len(st.stack) > 1:          # keep RESPONSE at the bottom
                top = st.stack.pop()
                if top.finish_reason is None:
                    top.finish_reason = "unclosed"
            st.finish_reason = reason
            st.done = True
            if st.stack and st.stack[0].kind is SegmentKind.RESPONSE:
                resp = st.stack.pop(0)
                if resp.finish_reason is None:
                    resp.finish_reason = reason
        return st

    def close_on_stop(self, reason: str) -> ParseState:
        """Close the segment tree after a runtime-plugin stop.

        Unlike finalize(), this NEVER touches the raw buffer: an element
        rejected by a voter must not leak into the committed output
        (rule 8 applies to clean ends only). Every still-open segment
        closes with finish_reason="unclosed"; the implicit RESPONSE
        segment and the state itself take the protocol ``reason``.
        """
        st = self.state
        if st.done:
            return st
        # Discard the un-voted tail without advancing token_offset —
        # those chunks were never delivered as elements.
        st.buffer = ""
        self._chunks_in_buffer = 0
        st.status = ParseStatus.WORD
        st.word = None
        while len(st.stack) > 1:          # keep RESPONSE at the bottom
            top = st.stack.pop()
            if top.finish_reason is None:
                top.finish_reason = "unclosed"
        st.finish_reason = reason
        st.done = True
        if st.stack and st.stack[0].kind is SegmentKind.RESPONSE:
            resp = st.stack.pop(0)
            if resp.finish_reason is None:
                resp.finish_reason = reason
        return st

    def response_text(self) -> str:
        """Think-free final text: concatenation of top-level RESPONSE
        segments (what PostPlugins receive as the clean response)."""
        return "".join(s.text for s in self.state.segments
                       if s.kind is SegmentKind.RESPONSE).strip()

    # ------------------------------------------------------------------
    # Element extraction
    # ------------------------------------------------------------------
    def _next_event(self) -> Optional[Tuple[ParseStatus, Optional[str]]]:
        """Extract the next ready element from the head of the buffer."""
        st = self.state
        buf = st.buffer
        m = TAG_RE.search(buf)
        if m is not None:
            canonical = self._fuzzy_tag(m.group(2))
            if canonical is None:
                # Unknown tag = ordinary text (whitelist is the filter).
                # Emit the closed prefix word if there is one, otherwise
                # let the '<...>' fragment ride on inside the word buffer.
                return self._pop_closed_word()
            prefix = buf[:m.start()]
            attrs = m.group(3) or ""
            body = (("/" if m.group(1) else "") + canonical
                    + ("/" if _SELF_CLOSING_RE.search(attrs) else ""))
            # Flush every fully-closed word in the prefix first.
            head = prefix
            st.buffer = head
            saved = buf[m.start():]           # raw tag + tail, for later
            ev = self._pop_closed_word()
            if ev is not None:
                # keep only the UNCLOSED remainder of the prefix + tag
                st.buffer = st.buffer + saved
                return ev
            # nothing closable before the tag → emit SPECIAL now
            st.buffer = buf[m.end():]
            self._chunks_in_buffer = max(0, self._chunks_in_buffer - 1)
            st.status = ParseStatus.SPECIAL
            st.word = body
            return (ParseStatus.SPECIAL, body)
        # No complete tag. If an unterminated '<...' tail could still
        # become a whitelisted tag, hold it as PARTIAL (chunk-boundary
        # ambiguity, model doc §v2 п.4); otherwise process words.
        lt = buf.rfind("<")
        if lt != -1 and ">" not in buf[lt:] and self._could_become_tag(buf[lt:]):
            head = buf[:lt]
            if WORD_BOUNDARY_RE.search(head):
                st.buffer = head
                ev = self._pop_closed_word()
                if ev is not None:
                    st.buffer = (st.buffer + buf[lt:]) if st.buffer else buf[lt:]
                    return ev
            return None
        return self._pop_closed_word()

    def _could_become_tag(self, tail: str) -> bool:
        """Does the incomplete '<...' tail plausibly prefix a known tag?"""
        core = tail.lstrip("<").lstrip("/")
        if not core or len(core) > 16:
            return False
        if not re.match(r"[a-zA-Z_][\w\-]*$", core):
            return False
        low = core.lower()
        for w in self.whitelist:
            if w.startswith(low) or _levenshtein(low, w[:len(low)]) \
                    <= self.fuzzy_max_distance:
                return True
        return False

    def _pop_closed_word(self) -> Optional[Tuple[ParseStatus, Optional[str]]]:
        """Cut the first boundary-closed word off the buffer head."""
        st = self.state
        m = WORD_BOUNDARY_RE.search(st.buffer)
        if m is None:
            return None
        word = st.buffer[:m.start()].strip()
        st.buffer = st.buffer[m.end():]
        self._chunks_in_buffer = max(0, self._chunks_in_buffer - 1)
        if not word:                          # leading-only boundary
            return self._pop_closed_word() if st.buffer else None
        st.status = ParseStatus.WORD
        st.word = word
        return (ParseStatus.WORD, word)

    def _flush_buffer_as_word(self) -> Tuple[ParseStatus, Optional[str]]:
        """Rule 9 overflow: whole buffer becomes one WORD."""
        st = self.state
        word = st.buffer.strip()
        st.buffer = ""
        self._chunks_in_buffer = 0
        st.status = ParseStatus.WORD
        st.word = word
        return (ParseStatus.WORD, word)

    # ------------------------------------------------------------------
    # Tag recognition & segment application
    # ------------------------------------------------------------------
    def _fuzzy_tag(self, name: str) -> Optional[str]:
        """Resolve a possibly-misspelled tag name against the whitelist."""
        low = name.lower()
        if low in self.whitelist:
            return low
        hits = self._bktree.find(low, self.fuzzy_max_distance)
        if hits:
            return min(hits, key=lambda h: _levenshtein(low, h))
        return None

    @staticmethod
    def _is_self_closing_body(body: str) -> bool:
        return body.endswith("/") and not body.startswith("/")

    def _close_top(self, reason: str) -> None:
        top = self.state.stack.pop() if len(self.state.stack) > 1 else None
        if top is not None and top.finish_reason is None:
            top.finish_reason = reason

    def _apply_special(self, body: str) -> None:
        """Commit a SPECIAL element: push/pop segments (rules 3–5, 7)."""
        st = self.state
        bare = body.rstrip("/")
        closing = bare.startswith("/")
        name = bare.lstrip("/")
        canonical = self._fuzzy_tag(name)
        if canonical is None:                 # defensive: text fallback
            target = st.current or self._ensure_response_segment()
            self._append_noise(target, f"<{body}> ")
            return
        kind = self.tag_kinds.get(canonical, SegmentKind(canonical))
        if self._is_self_closing_body(body):  # rule 5: stack untouched
            seg = Segment(kind=kind, finish_reason="stop")
            st.segments.append(seg)
            return
        if closing:
            # tolerant close (rule 3): pop up to the matching ancestor,
            # inner ones get "unclosed"; matching one gets "stop".
            for idx in range(len(st.stack) - 1, -1, -1):
                if st.stack[idx].kind is kind:
                    while len(st.stack) - 1 > idx:
                        inner = st.stack.pop()
                        if inner.finish_reason is None:
                            inner.finish_reason = "unclosed"
                    top = st.stack.pop()
                    if top.finish_reason is None:
                        top.finish_reason = "stop"
                    return
            # stray closer (no opener anywhere) → noise, kept as text
            target = st.current or self._ensure_response_segment()
            self._append_noise(target, f"</{canonical}> ")
            return
        # opening tag
        # rule 4: tool_call inside think — anomaly
        if (kind is SegmentKind.TOOL_CALL
                and any(s.kind is SegmentKind.THINK for s in st.stack)):
            while len(st.stack) > 1:
                top = st.stack.pop()
                if top.finish_reason is None:
                    top.finish_reason = ("aborted"
                                         if top.kind is SegmentKind.THINK
                                         else "unclosed")
            seg = Segment(kind=kind)
            st.segments.append(seg)
            st.stack.append(seg)
            self.tool_call_payload = seg      # kept for the next run() step
            if self.on_anomaly is not None:
                self.on_anomaly(seg)
            return
        # depth limit (rule 3): deeper than allowed → literal text
        if len(st.stack) >= self.max_depth:
            target = st.current or self._ensure_response_segment()
            self._append_noise(target, f"<{canonical}> ")
            return
        seg = Segment(kind=kind)
        st.segments.append(seg)
        st.stack.append(seg)

    def _append_noise(self, target: Segment, text: str) -> None:
        """Append a literal-text fragment (noise tag / stray closer /
        depth-limit fallback) as a Word — v5 keeps ``words`` the single
        primary representation of segment content."""
        word = Word(text=text, segment=target,
                    idx=self.state.word_count, is_hidden=target.is_hidden)
        target.words.append(word)
        target._text_cache = None
        self.state.words.append(word)
        self.state.word_count += 1

    def _ensure_response_segment(self) -> Segment:
        """RESPONSE == «вне тегов» (rule 2): implicit bottom-of-stack segment."""
        st = self.state
        if st.stack and st.stack[0].kind is SegmentKind.RESPONSE:
            return st.stack[0]
        resp = Segment(kind=SegmentKind.RESPONSE)
        st.segments.insert(0, resp)
        st.stack.insert(0, resp)
        return resp

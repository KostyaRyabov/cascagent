"""Unit tests for the plugin base classes (cascagent.plugins.base)
and the layered memory (cascagent.memory).

Covers: Plugin ABC contract (name/config/bind), PrePlugin abstractness +
user-only modification rule, RuntimePlugin op-types (continue/stop/error)
with should_stop/on_stream_end/reset GONE, PostPlugin abstract hook,
Memory single-store KV semantics with glob subscriptions,
Workspace/Project/Agent hierarchy with
per-level memories and agent-owned local cleanup.
No LLM required (§12.2 п.9 — fake LLM/memory only).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from cascagent.memory import Agent, Memory, Project, Workspace
from cascagent.plugins.base import Plugin, PostPlugin, PrePlugin, RuntimePlugin


class TestPluginBase:
    def test_root_plugin_has_no_hook_marker(self):
        """Order 2026-10-11: hook() is gone; the bare Plugin is a plain
        identity/config/bind container (instantiability no longer matters —
        sections carry the abstract hooks instead)."""
        assert not hasattr(Plugin, "hook")
        for cls in (PrePlugin, RuntimePlugin, PostPlugin):
            assert not hasattr(cls, "hook")

    def test_config_stored_verbatim(self):
        class P(PrePlugin):
            def on_input(self, messages, context):
                return messages

        p = P(threshold=0.75, top_k=3)
        assert p.config == {"threshold": 0.75, "top_k": 3}
        assert "threshold=0.75" in repr(p)

    def test_memories_start_unbound(self):
        class P(PostPlugin):
            def on_output(self, response, context):
                return {"response": response, "parsed": None,
                        "metadata": {}, "action": "accept"}

        p = P()
        assert p.global_memory is None and p.local_memory is None

    def test_bind_wires_both_memories_once(self):
        """Decision 2026-10-11: memories are attributes after bind(),
        not arguments of every hook call."""
        class P(PostPlugin):
            def on_output(self, response, context):
                self.local_memory.set("seen", response)
                return {"response": response, "parsed": None,
                        "metadata": {}, "action": "accept"}

        g, l = Memory("g"), Memory("l")
        p = P().bind(global_memory=g, local_memory=l)
        assert p.global_memory is g and p.local_memory is l
        p.on_output("x", {})
        assert l.get("seen") == "x"

    def test_no_reset_anywhere(self):
        """reset() was removed from the plugin API — cleanup is the
        agent's job (local namespace wipe at run())."""
        for cls in (Plugin, PrePlugin, RuntimePlugin, PostPlugin):
            assert not hasattr(cls, "reset")

    def test_section_names(self):
        assert Plugin.name == "base"
        assert PrePlugin.name == "base_pre"
        assert RuntimePlugin.name == "base_runtime"
        assert PostPlugin.name == "base_post"

    def test_hierarchy(self):
        assert issubclass(PrePlugin, Plugin)
        assert issubclass(RuntimePlugin, Plugin)
        assert issubclass(PostPlugin, Plugin)
        # InitPlugin is gone — its role folded into PrePlugin.
        assert not hasattr(sys.modules["cascagent.plugins.base"], "InitPlugin")


class TestPrePlugin:
    def test_abstract_on_input(self):
        with pytest.raises(TypeError):
            PrePlugin()

    def test_modifies_only_user_messages(self):
        """Contract (2026-10-11): enricher rewrites user turns only —
        system/assistant must stay byte-identical (prefix cache, P8).
        Signature carries NO memory/session args (bound via bind())."""
        class Enricher(PrePlugin):
            def on_input(self, messages, context):
                return [dict(m, content=m["content"] + "\n[дополнено]")
                        if m["role"] == "user" else m
                        for m in messages]

        msgs = [{"role": "system", "content": "hi"},
                {"role": "user", "content": "task"}]
        out = Enricher().on_input(msgs, {})
        assert out[0]["content"] == "hi"          # system untouched
        assert out[1]["content"] == "task\n[дополнено]"


class TestRuntimePlugin:
    class Bare(RuntimePlugin):
        pass  # no abstract methods — whole section has defaults

    def test_defaults_pass_through(self):
        p = self.Bare()
        assert p.on_upload(None) == {"op": "continue"}
        # should_stop is GONE — stop semantics live in on_upload ops.
        assert not hasattr(RuntimePlugin, "should_stop")
        # on_stream_end is GONE — stream finalization is PostPlugin's job.
        assert not hasattr(RuntimePlugin, "on_stream_end")
        # on_token is GONE (2026-10-12) — plugins vote on parsed elements,
        # raw tokens belong to the built-in StreamParser service.
        assert not hasattr(RuntimePlugin, "on_token")

    def test_op_types_shape(self):
        from cascagent.stream_parser import ParseState, ParseStatus

        class Stopper(self.Bare):
            def on_upload(self, state):
                if state.word == "STOP":
                    return {"op": "stop", "data": "tail"}
                if state.word == "!!!":
                    return {"op": "error", "data": "corrupted"}
                return {"op": "continue"}

        p = Stopper()
        mk = lambda w: ParseState(status=ParseStatus.WORD, word=w)
        assert p.on_upload(mk("x")) == {"op": "continue"}
        r = p.on_upload(mk("STOP"))
        assert r["op"] == "stop" and r["data"] == "tail"
        r = p.on_upload(mk("!!!"))
        assert r["op"] == "error" and r["data"] == "corrupted"


class TestPostPlugin:
    def test_abstract_on_output(self):
        with pytest.raises(TypeError):
            PostPlugin()

    def test_minimal_concrete(self):
        class Echo(PostPlugin):
            name = "echo"

            def on_output(self, response, context):
                return {"response": response, "parsed": None,
                        "metadata": {}, "action": "accept"}

        r = Echo().on_output("text", {})
        assert r["action"] == "accept" and r["response"] == "text"


class TestMemory:
    def test_set_get_default(self):
        m = Memory()
        assert m.get("nope", 42) == 42
        m.set("k", 1)
        assert m.get("k") == 1

    def test_update_with_default(self):
        m = Memory()
        assert m.update("cnt", lambda x: x + 1, default=0) == 1
        assert m.update("cnt", lambda x: x + 1, default=0) == 2

    def test_clear_empties_the_store(self):
        m = Memory()
        m.set("a", 1)
        m.clear()
        assert m.get("a") is None

    def test_no_scope_bucket_all_apis(self):
        """Decision 2026-10-11: one Memory = one store. No scopes, no
        buckets, no all(), no on_change in __init__."""
        m = Memory()
        assert not hasattr(m, "all")
        assert not hasattr(m, "clear_local")
        assert not hasattr(m, "_bucket")
        assert not hasattr(m, "default_scope")
        import inspect
        sig = inspect.signature(Memory.__init__)
        assert "on_change" not in sig.parameters
        assert "scope" not in inspect.signature(m.set).parameters

    def test_glob_subscribe_matches_pattern(self):
        m = Memory()
        seen = []
        m.subscribe("tasks:*", lambda k, v: seen.append((k, v)))
        m.set("tasks:42", {"brief": "x"})     # matches
        m.set("history", [1])                  # does NOT match
        assert seen == [("tasks:42", {"brief": "x"})]

    def test_star_pattern_replaces_on_change(self):
        m = Memory()
        seen = []
        m.subscribe("*", lambda k, v: seen.append(k))
        m.set("a", 1)
        m.update("b", lambda x: (x or 0) + 1, default=0)
        assert seen == ["a", "b"]

    def test_get_does_not_notify(self):
        m = Memory()
        fired = []
        m.subscribe("*", lambda k, v: fired.append(k))
        m.get("missing")
        assert fired == []

    def test_clear_is_silent(self):
        m = Memory()
        fired = []
        m.subscribe("*", lambda k, v: fired.append(k))
        m.set("a", 1)
        m.clear()
        assert fired == ["a"]          # only the write, not the wipe

    def test_unsubscribe(self):
        m = Memory()
        fired = []
        cb = lambda k, v: fired.append(k)
        m.subscribe("x:*", cb)
        assert m.unsubscribe("x:*") == 1
        m.set("x:1", 1)
        assert fired == []


class TestHierarchy:
    def _build(self):
        ws = Workspace("ws")
        pr = Project("pr", workspace=ws)
        ws.add_project(pr)
        ag = Agent("ag", project=pr)
        pr.add_agent(ag)
        return ws, pr, ag

    def test_each_level_owns_its_memory(self):
        ws, pr, ag = self._build()
        assert isinstance(ws.memory, Memory)
        assert isinstance(pr.memory, Memory)
        assert ws.memory is not pr.memory
        # agent owns TWO separate memories of its own
        assert ag.global_memory is not ag.local_memory

    def test_links_are_references_not_copies(self):
        ws, pr, ag = self._build()
        assert pr.workspace_memory is ws.memory
        assert ag.project_memory is pr.memory
        assert ag.workspace_memory is ws.memory

    def test_plugins_bound_to_agent_memories(self):
        class Echo(PostPlugin):
            def on_output(self, response, context):
                return {"response": response, "parsed": None,
                        "metadata": {}, "action": "accept"}

        ws, pr, ag = self._build()
        p = ag.add_plugin(Echo())
        assert p.global_memory is ag.global_memory
        assert p.local_memory is ag.local_memory

    def test_run_wipes_local_before_iteration(self):
        """The agent — not plugins — cleans state between runs()."""
        class NoteTaker(PrePlugin):
            def on_input(self, messages, context):
                self.local_memory.set("touched", True)
                return messages

        ws, pr, ag = self._build()
        ag.llm = lambda msgs: "ok"
        ag.add_plugin(NoteTaker())
        ag.run([{"role": "user", "content": "q"}])
        assert ag.local_memory.get("touched") is True
        ag.local_memory.set("leftover", 1)     # simulate stale scratch
        ag.run([{"role": "user", "content": "q"}])
        assert ag.local_memory.get("leftover") is None   # wiped by run()

    def test_global_memory_survives_iterations(self):
        class Counter(RuntimePlugin):
            def on_upload(self, state):
                n = self.global_memory.get("units", 0)
                self.global_memory.set("units", n + 1)
                return {"op": "continue"}

        ws, pr, ag = self._build()
        ag.llm = iter(["a ", "b "])
        ag.add_plugin(Counter())
        ag.run([{"role": "user", "content": "q"}])
        assert ag.global_memory.get("units") == 2
        ag.llm = iter(["c "])
        ag.run([{"role": "user", "content": "q"}])
        assert ag.global_memory.get("units") == 3       # survived the wipe

    def test_full_loop_accept_path(self):
        class Echo(PostPlugin):
            def on_output(self, response, context):
                return {"response": response.upper(), "parsed": None,
                        "metadata": {}, "action": "accept"}

        ws, pr, ag = self._build()
        ag.llm = lambda msgs: "hello"
        ag.add_plugin(Echo())
        res = ag.run([{"role": "user", "content": "q"}])
        assert res["response"] == "HELLO" and res["action"] == "accept"

    def test_runtime_stop_without_error(self):
        """v3 API (2026-10-12): RuntimePlugin votes on parsed elements via
        ``on_upload(state)`` — no raw ``on_token(token, accumulated)``.
        A non-continue vote means the element is NOT committed to the
        segment tree (docs/stream_parser_model.md §7 commit rule), so the
        stop-marker word itself never reaches the response.

        Chunk layout mirrors real SSE deltas: words close only on the
        boundary chunk ('STOP' closes when ' junk' arrives). NOTE (found by
        this test): after a runtime stop the agent still flushes the raw
        buffer tail into the segment tree in finalize() — 'junk' lands in
        the response although the stream was cut short. Pinned as-is for
        v0.1; to be revisited when client.py/kanban land (roadmap фаза 2)."""
        seen: list = []

        class Stopper(RuntimePlugin):
            def on_upload(self, state):
                seen.append(state.word)
                if state.word == "STOP":
                    return {"op": "stop", "data": ""}
                return {"op": "continue"}

        ws, pr, ag = self._build()
        ag.add_plugin(Stopper())
        ag.llm = iter(["go ", "STOP", " junk"])
        res = ag.run([{"role": "user", "content": "q"}])
        assert seen[-1] == "STOP"                # plugin saw the element
        assert res["response"] == "go junk"      # STOP not committed...
        assert res["finish_reason"] == "stop"    # stop WITHOUT error

    def test_runtime_error_raises(self):
        """Guardian-style runtime veto: ``{"op": "error"}`` surfaces as
        RuntimeError from ``Agent.run()`` (memory.py, chunk-iterator path).
        Votes happen per CLOSED element only — rule 1: punctuation is not a
        unit type, it merely closes the current word and is DROPPED from the
        payload (chunk 'oops!' yields WORD 'oops' — no trailing '!'), so a
        guardian must veto on the bare corrupted word itself."""
        class Guardian(RuntimePlugin):
            def on_upload(self, state):
                if state.word == "corrupted":
                    return {"op": "error", "data": "corrupted output"}
                return {"op": "continue"}

        ws, pr, ag = self._build()
        ag.add_plugin(Guardian())
        ag.llm = iter(["ok ", "corrupted!", " b"])   # WORDs: 'ok', 'corrupted'
        with pytest.raises(RuntimeError, match="corrupted"):
            ag.run([{"role": "user", "content": "q"}])

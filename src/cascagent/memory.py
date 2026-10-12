"""Layered memory of the cascagent architecture (v3).

Hierarchy (leader decisions 2026-10-11):

    Workspace ── owns ──> Memory   (cross-project state)
    Project   ── owns ──> Memory   (task tree, history; link to workspace)
    Agent     ── owns TWO memories of its OWN:
                        global_memory — agent-level state SURVIVING
                                        iterations (counters, caches);
                        local_memory  — per-iteration namespace shared by
                                        all plugins during one run();
                                        the AGENT wipes it at run() start.

One class ``Memory`` = one flat KV store that answers for ITSELF only:
no scopes, no buckets inside a single instance. The four levels above
are simply separate instances; child classes hold REFERENCES to the
memories of the levels above (never copies).

Observation: subscribers are registered with fnmatch-glob patterns over
key names (``subscribe("tasks:*", cb)``), so there is no need for a
global on_change in __init__ — a "*" pattern covers every key.

No persistence yet (sqlite lands later behind the same API);
single-threaded by design (P7).
"""

from __future__ import annotations

import fnmatch
from typing import Any, Callable, Dict, List, Optional


class Memory:
    """A flat KV store with glob-pattern subscriptions.

    Self-contained: exactly ONE dict per instance — scope separation
    (agent global vs local) is achieved by owning two instances, not by
    two dicts inside one. No ``all()`` either: introspection goes through
    subscriptions, and iteration logic that needs the whole picture keeps
    its own value under a single key.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._data: Dict[str, Any] = {}
        #: [(glob pattern, callback)] — fired on set.
        self._subscribers: List[tuple] = []

    # -- CRUD -----------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        """Write ``key`` and notify matching subscribers."""
        self._data[key] = value
        self._notify(key, value)

    def get(self, key: str, default: Any = None) -> Any:
        """Read ``key`` (``default`` when absent). Never fires callbacks."""
        return self._data.get(key, default)


    def clear(self) -> None:
        """Drop ALL keys silently (no per-key events — a bulk wipe is not
        N changes; called by agent.run() on local_memory each iteration)."""
        self._data.clear()

    # -- observation (glob subscriptions) --------------------------------
    def subscribe(self, pattern: str,
                  callback: Callable[[str, Any], None]) -> None:
        """Watch keys matching an fnmatch glob.

        Patterns: "tasks:*" (every task node), "*.error" (any error flag),
        "*" watches everything (replaces the old global on_change).
        Callback signature: ``callback(key, new_value)`` — fired after
        set, NOT after clear().
        """
        self._subscribers.append((pattern, callback))

    def unsubscribe(self, pattern: str,
                    callback: Optional[Callable] = None) -> int:
        """Remove subscription(s) by pattern (and optionally callback).
        Returns the number of removed entries."""
        before = len(self._subscribers)
        self._subscribers = [
            (p, cb) for p, cb in self._subscribers
            if not (p == pattern and (callback is None or cb is callback))
        ]
        return before - len(self._subscribers)

    def _notify(self, key: str, value: Any) -> None:
        for pattern, callback in self._subscribers:
            if fnmatch.fnmatchcase(key, pattern):
                callback(key, value)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"<Memory {self.name!r} keys={len(self._data)} "
                f"subs={len(self._subscribers)}>")


class Workspace:
    """Top level: all projects of one user/session."""

    def __init__(self, name: str = "workspace", **config: Any) -> None:
        self.name = name
        self.config = dict(config)
        self.projects: Dict[str, "Project"] = {}
        #: Own memory — the only level this class holds directly.
        self.memory = Memory(name=f"{name}.workspace")

    def add_project(self, project: "Project") -> "Project":
        project.workspace = self
        self.projects[project.name] = project
        return project


class Project:
    """Middle level: one ongoing conversation/goal.

    Owns its memory; holds a REFERENCE to the workspace memory above.
    """

    def __init__(self, name: str, workspace: Optional[Workspace] = None,
                 **config: Any) -> None:
        self.name = name
        self.config = dict(config)
        self.agents: Dict[str, "Agent"] = {}
        self.workspace = workspace
        #: Own project-level memory (task tree, history, decomposer state).
        self.memory = Memory(name=f"{name}.project")

    @property
    def workspace_memory(self) -> Optional[Memory]:
        """Link upward — reads/writes meant for every project land there."""
        return self.workspace.memory if self.workspace is not None else None

    def add_agent(self, agent: "Agent") -> "Agent":
        agent.project = self
        self.agents[agent.name] = agent
        return agent


class Agent:
    """Bottom level: one runnable loop (LLM + plugins).

    The agent owns TWO Memory instances of its own (decision 2026-10-11):
    - ``global_memory``: agent-level state surviving iterations;
    - ``local_memory`` : per-iteration namespace shared by all plugins;
      cleared by ``run()`` itself — plugins have no reset() anymore.
    Both are BOUND into every plugin at add_plugin(), so hot-path hooks
    never carry memory arguments. Links upward are references:
    ``project_memory`` / ``workspace_memory`` properties.
    """

    def __init__(self, name: str, project: Optional["Project"] = None,
                 llm: Any = None, pre_plugins=(), runtime_plugins=(),
                 post_plugins=(), parser_config: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self.llm = llm
        self.project = project
        #: Agent's own cross-iteration state.
        self.global_memory = Memory(name=f"{name}.global")
        #: Agent's own per-iteration namespace (plugins talk through it).
        self.local_memory = Memory(name=f"{name}.local")

        #: Plugin lists — each plugin gets BOTH memories bound into it.
        self.pre_plugins: List[Any] = []
        self.runtime_plugins: List[Any] = []
        self.post_plugins: List[Any] = []
        for plugin in (*pre_plugins, *runtime_plugins, *post_plugins):
            self.add_plugin(plugin)   # add_plugin routes by section type

        #: Built-in stream-structuring service (NOT a plugin, decision
        #: 2026-10-12; spec docs/stream_parser_model.md). Fresh instance
        #: per run(); config comes from the [parser] section (config.md).
        self.parser_config: Dict[str, Any] = dict(parser_config or {})

    def _make_parser(self):
        """Instantiate StreamParser from ``parser_config`` (agent-owned)."""
        from cascagent.stream_parser import StreamParser
        cfg = self.parser_config
        kwargs: Dict[str, Any] = {}
        if "whitelist" in cfg:
            kwargs["whitelist"] = tuple(cfg["whitelist"])
        for key in ("max_depth", "fuzzy_max_distance", "max_buffer_chunks"):
            if key in cfg:
                kwargs[key] = cfg[key]
        return StreamParser(**kwargs)

    # -- plugin wiring --------------------------------------------------
    def add_plugin(self, plugin: Any) -> Any:
        """Attach a plugin and bind the agent's memories into it.

        Binding happens ONCE at initialization so hot-path hooks stay
        argument-light: ``on_token(token, accumulated)`` instead of
        dragging session+memory through every single token.
        """
        from cascagent.plugins.base import PrePlugin, RuntimePlugin, PostPlugin
        # Bind ALL four hierarchy memories: the agent's own two plus the
        # upward references (project/workspace may be None if unwired).
        plugin.bind(workspace_memory=self.workspace_memory,
                    project_memory=self.project_memory,
                    global_memory=self.global_memory,
                    local_memory=self.local_memory)
        if isinstance(plugin, PrePlugin):
            self.pre_plugins.append(plugin)
        elif isinstance(plugin, RuntimePlugin):
            self.runtime_plugins.append(plugin)
        elif isinstance(plugin, PostPlugin):
            self.post_plugins.append(plugin)
        else:
            raise TypeError(f"not a plugin section subclass: {plugin!r}")
        return plugin

    # -- upward references (links, not copies) --------------------------
    @property
    def project_memory(self) -> Optional[Memory]:
        return self.project.memory if self.project is not None else None

    @property
    def workspace_memory(self) -> Optional[Memory]:
        return self.project.workspace_memory if self.project is not None else None

    # -- one full iteration ----------------------------------------------
    def run(self, messages: List[Dict[str, str]],
            context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """PRE → LLM → RUNTIME(stream via built-in StreamParser) → POST.

        First act: wipe local scope. The agent owns the StreamParser
        service (docs/stream_parser_model.md): raw chunks become WORD /
        SPECIAL elements; every element is delivered to ALL runtime
        plugins (on_upload) and applied to the segment tree ONLY if all
        of them vote continue.
        """
        self.local_memory.clear()
        context = dict(context or {})

        for plugin in self.pre_plugins:
            messages = plugin.on_input(messages, context)

        parser = self._make_parser()
        response = ""
        if self.llm is not None:
            # LLM seam: callable(messages) -> str | chunk iterator, or a
            # bare chunk iterator (tests/fakes). Chunks are raw SSE
            # delta.content pieces — the parser assembles words from them.
            gen = self.llm(messages) if callable(self.llm) else self.llm
            stop: Optional[Dict[str, Any]] = None
            if isinstance(gen, str):          # fake/test LLM: whole text
                for ev in parser.feed(gen):
                    parser.commit(ev)         # no runtime vote on a string LLM
                parser.finalize("stop")
            else:                             # real LLM: chunk iterator
                for chunk in gen:
                    if chunk == "[DONE]":
                        break
                    response += chunk
                    for ev in parser.feed(chunk):
                        status, payload = ev
                        parser.state.status = status
                        parser.state.word = payload
                        voted = None
                        for rt in self.runtime_plugins:
                            res = rt.on_upload(parser.state)
                            op = res.get("op", "continue")
                            if op != "continue":
                                voted = {"op": op, **res}
                                break
                        if voted is not None:
                            stop = voted   # element NOT committed (vote rule)
                            break
                        parser.commit(ev)
                    if stop is not None:
                        break
                parser.finalize(stop["op"] if stop else "stop")
                if stop is not None and stop["op"] == "error":
                    raise RuntimeError(f"runtime stopped with error: {stop}")
            response = parser.response_text() or response

        #: Live parser state is passed to plugins directly via context —
        #: it is an internal mechanism, not memory data (docs/plugins.md §2.4).
        context["parse_state"] = parser.state
        context.setdefault("response", response)
        context.setdefault("think", "".join(
            s.text for s in parser.state.segments
            if s.kind.name == "THINK").strip())
        result: Dict[str, Any] = {"response": response, "context": context,
                                  "segments": parser.state.segments,
                                  "finish_reason": parser.state.finish_reason}
        for plugin in self.post_plugins:
            r = plugin.on_output(response, context)
            response = r.get("response", response)
            result.update({"parsed": r.get("parsed"),
                           "metadata": r.get("metadata"),
                           "action": r.get("action", "accept")})
            if result["action"] != "accept":
                break
        result["response"] = response
        return result

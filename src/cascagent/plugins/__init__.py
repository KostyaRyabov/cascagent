"""Plugin layer of cascagent (architecture v3).

Base classes (docs/plugins.md §6.3, renamed per the decisions of 2026-10-11):
``Plugin`` — common ancestor (name/config/bind of all four memories, no
``hook()`` marker), ``PrePlugin``, ``RuntimePlugin``, ``PostPlugin``. The
former ``InitPlugin`` section is removed — its role is covered by
``PrePlugin``. Concrete plugins carry the ``Plugin`` suffix:
``TaskParserPlugin`` (decomposition parsing + duplicate detection,
plugins/task_parser.py). The former standalone module ``cascagent.parser``
and ``detector.py`` are deleted. Think/response splitting is NOT a plugin
anymore: ``OutputSplitterPlugin`` was removed (2026-10-12) — stream
structure is now the job of the built-in agent service ``StreamParser``
(cascagent.stream_parser, spec: docs/stream_parser_model.md).
"""

from .base import Plugin, PrePlugin, RuntimePlugin, PostPlugin
from .task_parser import TaskParserPlugin

__all__ = ["Plugin", "PrePlugin", "RuntimePlugin", "PostPlugin",
           "TaskParserPlugin"]

"""Plugin layer of cascagent (architecture v3).

Base classes (docs/plugins.md §6.3, renamed per the decisions of 2026-10-11):
``Plugin`` — common ancestor (name/config/bind of all four memories, no
``hook()`` marker), ``PrePlugin``, ``RuntimePlugin``, ``PostPlugin``. The
former ``InitPlugin`` section is removed — its role is covered by
``PrePlugin``. Concrete plugins carry the ``Plugin`` suffix:
``OutputSplitterPlugin`` (think/response split, plugins/splitter.py) and
``TaskParserPlugin`` (decomposition parsing + duplicate detection,
plugins/task_parser.py). The former standalone module ``cascagent.parser``
and ``detector.py`` are deleted.
"""

from .base import Plugin, PrePlugin, RuntimePlugin, PostPlugin
from .splitter import OutputSplitterPlugin
from .task_parser import TaskParserPlugin

__all__ = ["Plugin", "PrePlugin", "RuntimePlugin", "PostPlugin",
           "OutputSplitterPlugin", "TaskParserPlugin"]

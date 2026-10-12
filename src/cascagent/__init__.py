"""cascagent — multi-agent recursive task decomposition (protocol v2).

Stage 1 (Foundation): models (Task/TaskStatus), plugins (base classes +
TaskParserPlugin — decomposition parsing and duplicate detection,
including the absorbed DuplicateDetector rules), history. The former
standalone parser.py module is deleted. Think/response splitting lives
neither in plugins nor in TaskParserPlugin: it is a built-in agent
service (StreamParser), not a plugin; OutputSplitterPlugin was removed.

CPU-algorithm layer (Этап 4, started): bk_tree — BK-tree fuzzy search with
pluggable distance metrics, usage statistics and rebalancing (P4 tool-name
resolution, fix_typo); see docs/algorithms.md §1.
"""

from cascagent.bk_tree import (
    BKNode,
    BKTree,
    BKTreeConfig,
    BKTreeStats,
    DistanceMetric,
    HammingDistance,
    JaccardDistance,
    LevenshteinDistance,
    SemanticDistance,
    fix_typo,
)

__version__ = "0.2.0"

__all__ = [
    "BKNode",
    "BKTree",
    "BKTreeConfig",
    "BKTreeStats",
    "DistanceMetric",
    "HammingDistance",
    "JaccardDistance",
    "LevenshteinDistance",
    "SemanticDistance",
    "fix_typo",
]

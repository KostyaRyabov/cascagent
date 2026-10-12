"""BK-tree — fuzzy search over a metric space (docs/algorithms.md §1, P4).

Burkhard–Keller tree: O(log n) fuzzy lookup instead of an O(n) scan.
Used by the CPU layer for tool-name resolution with typos (P4), CLI command
lookup and semantic dictionary search — so that MCP tool schemas never enter
the LLM prompt (cpu-offload.md §P4).

Specification source: «BK-tree: полная документация» (Chapter of the full
project documentation). The API below follows it exactly:

- ``DistanceMetric`` (ABC) with pluggable implementations:
  ``LevenshteinDistance`` / ``SemanticDistance`` / ``HammingDistance`` /
  ``JaccardDistance``;
- ``BKTreeConfig`` — analytics / rebalance knobs (metric is NOT part of the
  config — two separate constructor parameters);
- ``BKTreeStats`` — minimal usage statistics (read weights + depths);
- ``BKTree`` — add/append/query/contains/rebalance; thread-safe insertion.

Project-specific deviations from the doc listing (both mandated by
docs/algorithms.md §0):

1. stdlib fallback for ``Levenshtein`` (DP edit distance, ~15 lines) — the
   core must install and test without extras (algorithms.md §0 п.2);
2. ``normalize()`` (lower + collapse whitespace, algorithms.md §6.1) is
   applied to every incoming word in ``add``/``query``/``contains`` — the
   shared comparison canon of the whole CPU layer (algorithms.md §0 п.4).

``fix_typo`` (AGENTS.md §6: «BKTree + fix_typo») is a thin wrapper over
``query`` returning the nearest candidate or ``None``.
"""

from __future__ import annotations

import json
import re
import threading
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:  # extras: python-Levenshtein (fast C implementation)
    from Levenshtein import distance as _levenshtein_c
except ImportError:  # pragma: no cover - exercised only without extras
    _levenshtein_c = None


# ---------------------------------------------------------------------------
# Shared comparison canon (docs/algorithms.md §6.1): lower + collapsed spaces.
# Only this — stop words / lemmatization are deliberately NOT done in v0.x.
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+", flags=re.UNICODE)


def normalize(text: str) -> str:
    """Canonical form for all CPU-layer comparisons (algorithms.md §0 п.4)."""
    return _WS_RE.sub(" ", text.lower()).strip()


def levenshtein_distance(a: str, b: str) -> int:
    """Edit distance: python-Levenshtein when available, DP fallback otherwise."""
    if _levenshtein_c is not None:
        return _levenshtein_c(a, b)
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


# ---------------------------------------------------------------------------
# Distance metrics (documentation §3)
# ---------------------------------------------------------------------------

class DistanceMetric(ABC):
    """Abstract base for distance metrics.

    Contract: ``compute(a, b)`` is the only public method; implementations
    must route their result through ``_round_distance()`` before returning.
    Result: number >= 0 (int or float).
    """

    @abstractmethod
    def compute(self, a: str, b: str) -> Union[int, float]:
        """Distance between two strings."""

    def _round_distance(self, distance: Union[int, float]) -> Union[int, float]:
        """Rounding used for BK-tree grouping; identity by default."""
        return distance


class LevenshteinDistance(DistanceMetric):
    """Levenshtein edit distance. Integer >= 0. A true metric (triangle
    inequality holds), so BK pruning is exact.

    Use for: typos in short strings — tool/command names.
    """

    def compute(self, a: str, b: str) -> int:
        return self._round_distance(levenshtein_distance(a, b))

    def _round_distance(self, distance: Union[int, float]) -> int:
        return int(distance)


class SemanticDistance(DistanceMetric):
    """Cosine distance on embeddings: ``1 - cosine_similarity``, float in [0, 2].

    Args:
        embedding_fn: ``text -> numpy array``.
        embedding_cache: optional pre-filled cache {text: vector}.
        precision: decimal places of the rounded distance — controls how many
            children each node gets (fewer digits = fewer children = faster,
            coarser grouping).
    """

    def __init__(
        self,
        embedding_fn: Callable[[str], Any],
        embedding_cache: Optional[Dict[str, Any]] = None,
        precision: int = 2,
    ):
        self.embedding_fn = embedding_fn
        self.cache: Dict[str, Any] = dict(embedding_cache) if embedding_cache else {}
        self.precision = precision

    def _get_embedding(self, text: str):
        if text not in self.cache:
            self.cache[text] = self.embedding_fn(text)
        return self.cache[text]

    def compute(self, a: str, b: str) -> float:
        import numpy as np

        emb_a = self._get_embedding(a)
        emb_b = self._get_embedding(b)
        similarity = np.dot(emb_a, emb_b) / (
            np.linalg.norm(emb_a) * np.linalg.norm(emb_b)
        )
        return self._round_distance(1.0 - float(similarity))

    def _round_distance(self, distance: Union[int, float]) -> float:
        return round(float(distance), self.precision)

    def save_cache(self, path: Path) -> None:
        """Persist the embedding cache to disk (JSON, language-neutral)."""
        data = {text: list(map(float, emb)) for text, emb in self.cache.items()}
        Path(path).write_text(json.dumps(data), encoding="utf-8")

    def load_cache(self, path: Path) -> None:
        """Load the embedding cache from disk if it exists."""
        import numpy as np

        path = Path(path)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.cache = {text: np.array(emb) for text, emb in data.items()}


class HammingDistance(DistanceMetric):
    """Hamming distance — count of differing positions. Requires equal-length
    strings; raises ``ValueError`` otherwise. Integer >= 0.

    Use for: fixed-length strings (hashes, codes).
    """

    def compute(self, a: str, b: str) -> int:
        if len(a) != len(b):
            raise ValueError(
                f"Hamming distance requires equal length strings, "
                f"got {len(a)} and {len(b)}"
            )
        return self._round_distance(sum(c1 != c2 for c1, c2 in zip(a, b)))

    def _round_distance(self, distance: Union[int, float]) -> int:
        return int(distance)


class JaccardDistance(DistanceMetric):
    """Jaccard distance over character sets: ``1 - |A∩B| / |A∪B|``, float in
    [0, 1]. Ignores order and multiplicity.

    Use for: token/symbol sets where order does not matter.
    """

    def __init__(self, precision: int = 2):
        self.precision = precision

    def compute(self, a: str, b: str) -> float:
        set_a, set_b = set(a), set(b)
        union = len(set_a | set_b)
        if union == 0:
            return 0.0
        similarity = len(set_a & set_b) / union
        return self._round_distance(1.0 - similarity)

    def _round_distance(self, distance: Union[int, float]) -> float:
        return round(float(distance), self.precision)


# ---------------------------------------------------------------------------
# Configuration (documentation §5)
# ---------------------------------------------------------------------------

@dataclass
class BKTreeConfig:
    """BK-tree configuration (analytics + rebalancing). The distance metric
    is NOT part of the config — it is a separate constructor argument."""

    enable_analytics: bool = True
    rebalance_check_interval: int = 1000          # check every N queries; 0 = off
    rebalance_quality_threshold: float = 0.1      # quality below this → rebalance
    auto_rebalance: bool = False                  # rebuild automatically on trigger
    on_need_rebalance: Optional[Callable[["BKTree", float], None]] = None


# ---------------------------------------------------------------------------
# Usage statistics (documentation §4)
# ---------------------------------------------------------------------------

class BKTreeStats:
    """Minimal usage statistics: per-word read weight (accumulated depth of
    every successful read) and per-word depth in the tree.

    Depth is stamped once at insertion and changes only on rebalance; frequent
    words should migrate closer to the root to reduce total access cost.
    Public API: 4 methods + the quality getter.
    """

    def __init__(self) -> None:
        self.read_weights: Counter = Counter()
        self.depths: Dict[str, int] = {}
        self.needs_rebalance: bool = False
        self.last_quality_score: float = 0.0

    def set_depth(self, word: str, depth: int) -> None:
        """Stamp the depth of a word (on insert and after every rebalance)."""
        self.depths[word] = depth

    def record_read(self, word: str) -> None:
        """Add the word's depth to its accumulated read weight."""
        depth = self.depths.get(word, 1)
        self.read_weights[word] += depth

    def get_sorted_by_weight(self) -> List[str]:
        """Words sorted by read weight, descending (rebalance order)."""
        return [word for word, _ in self.read_weights.most_common()]

    def get_quality_score(self) -> float:
        """Uniformity metric mean(weights) / sum(weights) in [0, 1].

        High = evenly distributed usage; low = one word dominates the load.
        0.0 when statistics are empty.
        """
        if not self.read_weights:
            return 0.0
        values = list(self.read_weights.values())
        total = sum(values)
        if total == 0:
            return 0.0
        mean = total / len(values)
        return mean / total

    def reset(self) -> None:
        """Full statistics reset."""
        self.read_weights = Counter()
        self.depths = {}
        self.needs_rebalance = False
        self.last_quality_score = 0.0


# ---------------------------------------------------------------------------
# BK-tree (documentation §6)
# ---------------------------------------------------------------------------

@dataclass
class BKNode:
    """One BK-tree node: a word plus children keyed by distance to parent."""

    word: str
    children: Dict[Any, "BKNode"] = field(default_factory=dict)


class BKTree:
    """Burkhard–Keller tree with pluggable distance metrics and optional
    usage analytics / rebalancing.

    Example:
        >>> tree = BKTree(distance_metric=LevenshteinDistance())
        >>> tree.append(["read_file", "write_file", "delete_file"])
        >>> tree.query("read_fiel", max_distance=2)
        [('read_file', 2)]
    """

    def __init__(
        self,
        distance_metric: Optional[DistanceMetric] = None,
        config: Optional[BKTreeConfig] = None,
    ):
        """
        Args:
            distance_metric: metric instance (default: LevenshteinDistance).
            config: tree configuration (default: BKTreeConfig()).
        """
        self.distance_metric = distance_metric or LevenshteinDistance()
        self.config = config or BKTreeConfig()

        self.root: Optional[BKNode] = None
        self.size: int = 0
        self._all_words: List[str] = []
        self._lock = threading.Lock()

        self.stats: Optional[BKTreeStats] = (
            BKTreeStats() if self.config.enable_analytics else None
        )
        self._queries_since_check: int = 0

    # ------------------------- insertion ------------------------------

    def add(self, word: str) -> None:
        """Insert one word (normalized; duplicates ignored).

        Stamps the word's depth into statistics exactly once. Thread-safe.
        """
        word = normalize(word)
        with self._lock:
            if self.root is None:
                self.root = BKNode(word=word)
                self.size = 1
                self._all_words.append(word)
                if self.stats:
                    self.stats.set_depth(word, depth=0)
                return

            node = self.root
            depth = 0
            while True:
                dist = self.distance_metric.compute(word, node.word)
                depth += 1
                if dist == 0:
                    return  # already present
                if dist in node.children:
                    node = node.children[dist]
                else:
                    node.children[dist] = BKNode(word=word)
                    self.size += 1
                    self._all_words.append(word)
                    if self.stats:
                        self.stats.set_depth(word, depth=depth)
                    return

    def append(self, words: List[str]) -> None:
        """Insert a list of words."""
        for word in words:
            self.add(word)

    # ---------------------------- search ------------------------------

    def query(
        self, word: str, max_distance: Union[int, float]
    ) -> List[Tuple[str, Union[int, float]]]:
        """All stored words within ``max_distance`` of ``word``.

        Returns ``(word, distance)`` pairs sorted by ascending distance.
        Records read statistics for every hit (when analytics enabled).
        """
        word = normalize(word)
        if self.root is None:
            return []

        results: List[Tuple[str, Union[int, float]]] = []
        self._search_recursive(self.root, word, max_distance, results)
        results.sort(key=lambda x: x[1])

        if self.stats and self.config.enable_analytics:
            for found_word, _ in results:
                self.stats.record_read(found_word)
            self._queries_since_check += 1
            self._maybe_check_rebalance()

        return results

    def contains(self, word: str) -> bool:
        """Exact membership test.

        O(1) via the depths dictionary when analytics is on; falls back to a
        tree walk otherwise.
        """
        word = normalize(word)
        if self.stats and self.config.enable_analytics:
            return word in self.stats.depths

        if self.root is None:
            return False
        node = self.root
        while True:
            dist = self.distance_metric.compute(word, node.word)
            if dist == 0:
                return True
            if dist in node.children:
                node = node.children[dist]
            else:
                return False

    def __contains__(self, word: str) -> bool:
        return self.contains(word)

    def __len__(self) -> int:
        return self.size

    def _search_recursive(
        self,
        node: BKNode,
        word: str,
        max_distance: Union[int, float],
        results: List[Tuple[str, Union[int, float]]],
    ) -> None:
        """Pruned traversal: triangle inequality restricts children to
        ``[d - m, d + m]`` where ``d`` is the distance to the current node."""
        dist = self.distance_metric.compute(word, node.word)
        if dist <= max_distance:
            results.append((node.word, dist))

        min_dist = dist - max_distance
        max_dist_range = dist + max_distance

        if isinstance(dist, int) and isinstance(max_distance, int):
            for d in range(int(min_dist), int(max_dist_range) + 1):
                if d in node.children:
                    self._search_recursive(
                        node.children[d], word, max_distance, results
                    )
        else:
            for d, child in node.children.items():
                if min_dist <= d <= max_dist_range:
                    self._search_recursive(child, word, max_distance, results)

    # -------------------------- rebalancing ----------------------------

    def _maybe_check_rebalance(self) -> None:
        if self.config.rebalance_check_interval == 0:
            return
        if self._queries_since_check >= self.config.rebalance_check_interval:
            self._queries_since_check = 0
            self._check_rebalance_needed()

    def _check_rebalance_needed(self) -> None:
        """Compute the quality score; flag / notify / auto-rebalance."""
        if not self.stats:
            return
        quality_score = self.stats.get_quality_score()
        self.stats.last_quality_score = quality_score

        if quality_score < self.config.rebalance_quality_threshold:
            self.stats.needs_rebalance = True
            if self.config.on_need_rebalance:
                self.config.on_need_rebalance(self, quality_score)
            if self.config.auto_rebalance:
                self.rebalance()

    def rebalance(self) -> None:
        """Full rebuild: hot words first → they end up closest to the root.

        1. take words ordered by descending read weight;
        2. append never-read words at the end;
        3. clear the tree and statistics completely;
        4. re-insert in the new order.
        """
        if self.stats:
            sorted_words = self.stats.get_sorted_by_weight()
        else:
            sorted_words = list(self._all_words)

        words_with_stats = set(sorted_words)
        for word in self._all_words:
            if word not in words_with_stats:
                sorted_words.append(word)

        self.root = None
        self.size = 0
        self._all_words = []
        if self.stats:
            self.stats.reset()

        for word in sorted_words:
            self.add(word)

    def needs_rebalance(self) -> bool:
        """Rebalance flag maintained by the periodic quality check."""
        return self.stats.needs_rebalance if self.stats else False

    # ----------------------------- utils -------------------------------

    def get_stats(self) -> Optional[BKTreeStats]:
        """Statistics object, or None when analytics is disabled."""
        return self.stats

    def clear_stats(self) -> None:
        """Reset statistics (fresh BKTreeStats instance)."""
        if self.config.enable_analytics:
            self.stats = BKTreeStats()


# ---------------------------------------------------------------------------
# fix_typo (AGENTS.md §6: «BKTree + fix_typo»)
# ---------------------------------------------------------------------------

def fix_typo(
    word: str,
    candidates: Union[BKTree, List[str]],
    max_distance: Union[int, float] = 2,
) -> Optional[str]:
    """Resolve a possibly misspelled ``word`` against known names.

    Returns the closest candidate within ``max_distance`` (normalized), or
    ``None`` when nothing matches. Accepts either a ready ``BKTree`` or a
    plain list of candidates (a throwaway Levenshtein tree is built then).
    """
    if not isinstance(candidates, BKTree):
        tree = BKTree(distance_metric=LevenshteinDistance(),
                      config=BKTreeConfig(enable_analytics=False))
        tree.append(list(candidates))
    else:
        tree = candidates
    results = tree.query(word, max_distance=max_distance)
    return results[0][0] if results else None

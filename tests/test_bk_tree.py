"""Unit tests for the BK-tree CPU module (cascagent.bk_tree).

Spec: «BK-tree: полная документация» + docs/algorithms.md §1.
Covers: distance metrics (Levenshtein/Semantic/Hamming/Jaccard, custom
subclass, _round_distance contract), BKTree add/append/query/contains
(normalization canon), depth stamping, statistics API (record_read /
get_sorted_by_weight / get_quality_score / reset), rebalance cycle
(manual, auto, callback), thread-safe insertion, fix_typo wrapper,
stdlib Levenshtein fallback.
No LLM required (algorithms.md §0 п.1 — deterministic CPU tests).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

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
    levenshtein_distance,
    normalize,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def brute_force(words, query, metric, max_distance):
    """Reference O(n) scan — oracle for pruning correctness."""
    return sorted(
        [(w, metric.compute(query, w)) for w in words
         if metric.compute(query, w) <= max_distance],
        key=lambda x: x[1],
    )


class TestMetrics:
    def test_levenshtein_known_values(self):
        m = LevenshteinDistance()
        assert m.compute("kitten", "sitting") == 3
        assert m.compute("read_file", "read_fiel") == 2
        assert m.compute("abc", "abc") == 0
        assert isinstance(m.compute("a", "b"), int)

    def test_hamming_equal_length(self):
        m = HammingDistance()
        assert m.compute("karolin", "kathrin") == 3
        assert m.compute("0000", "1111") == 4

    def test_hamming_raises_on_different_lengths(self):
        with pytest.raises(ValueError, match="equal length"):
            HammingDistance().compute("abc", "ab")

    def test_jaccard_char_sets(self):
        m = JaccardDistance(precision=2)
        assert m.compute("abc", "abc") == 0.0
        assert m.compute("", "") == 0.0
        # sets {a,b,c} vs {c,d}: intersection 1, union 4 → 1 - 0.25
        assert m.compute("abc", "cd") == 0.75

    def test_semantic_cosine_distance(self):
        vectors = {
            "cat": [1.0, 0.0],
            "kitty": [0.98, 0.02],
            "dog": [0.0, 1.0],
        }
        m = SemanticDistance(vectors.__getitem__, precision=2)
        assert m.compute("cat", "cat") == 0.0
        assert m.compute("cat", "kitty") < 0.1
        assert m.compute("cat", "dog") == 1.0

    def test_semantic_cache_and_persistence(self, tmp_path):
        calls = []

        def embed(text):
            calls.append(text)
            return [float(len(text)), 1.0]

        m = SemanticDistance(embed, precision=2)
        m.compute("aa", "ab")
        m.compute("aa", "ab")  # cached — no second encode burst per word
        assert len(calls) == 2  # one per distinct text

        path = tmp_path / "emb.json"
        m.save_cache(path)
        m2 = SemanticDistance(embed, precision=2)
        m2.load_cache(path)
        assert set(m2.cache) == {"aa", "ab"}
        assert m2.compute("aa", "ab") == m.compute("aa", "ab")

    def test_semantic_precision_groups_children(self):
        # vectors [1,0] vs [1,1]: cosine sim = 1/sqrt(2) ≈ 0.7071 → d ≈ 0.2929
        def embed(text):
            return [1.0, 0.0] if text == "a" else [1.0, 1.0]

        coarse = SemanticDistance(embed, precision=1)
        fine = SemanticDistance(embed, precision=3)
        assert coarse.compute("a", "b") == 0.3   # rounded to one decimal
        assert fine.compute("a", "b") == 0.293   # three decimals — finer groups

    def test_custom_metric_subclass(self):
        class LengthDiff(DistanceMetric):
            def compute(self, a, b):
                return self._round_distance(abs(len(a) - len(b)))

            def _round_distance(self, distance):
                return int(distance)

        m = LengthDiff()
        assert m.compute("aaa", "b") == 2
        with pytest.raises(TypeError):
            DistanceMetric()  # ABC cannot be instantiated

    def test_round_distance_hook_used(self):
        m = JaccardDistance(precision=1)
        # raw distance 1 - 2/3 = 0.333... → rounded to one decimal
        assert m.compute("ab", "abc") == 0.3


class TestBKTreeBasics:
    def test_empty_tree(self):
        tree = BKTree()
        assert len(tree) == 0
        assert tree.query("anything", max_distance=2) == []
        assert "x" not in tree
        assert tree.root is None

    def test_documentation_example(self):
        tree = BKTree()
        tree.append(["read_file", "write_file", "delete_file", "list_directory"])
        assert tree.query("read_fiel", max_distance=2) == [("read_file", 2)]
        assert ("write_file", 2) in tree.query("wriet_file", max_distance=2)
        assert "read_file" in tree
        assert "nonexistent" not in tree
        assert len(tree) == 4

    def test_add_deduplicates(self):
        tree = BKTree()
        tree.add("read_file")
        tree.add("read_file")
        tree.add("READ_FILE")  # normalization canon makes this a duplicate too
        assert len(tree) == 1

    def test_normalization_canon(self):
        assert normalize("  Read   FILE ") == "read file"
        tree = BKTree()
        tree.add("Read  File")
        assert tree.contains("read file")
        assert tree.query("read  fiel", max_distance=2) == [("read file", 2)]

    def test_query_sorted_by_distance(self):
        tree = BKTree()
        tree.append(["hello", "helo", "hallo", "world"])
        results = tree.query("helo", max_distance=2)
        distances = [d for _, d in results]
        assert distances == sorted(distances)
        assert results[0] == ("helo", 0)

    def test_pruning_matches_brute_force(self):
        words = ["read_file", "write_file", "delete_file", "list_directory",
                 "search_code", "run_shell", "create_dir", "move_item"]
        tree = BKTree(config=BKTreeConfig(enable_analytics=False))
        tree.append(words)
        metric = LevenshteinDistance()
        for q in ["read_fiel", "wriet", "delte", "lsit", "xyzzy", "run_shel"]:
            got = tree.query(q, max_distance=2)
            expected = brute_force([normalize(w) for w in words], q, metric, 2)
            assert sorted(got, key=lambda x: (x[1], x[0])) == \
                   sorted(expected, key=lambda x: (x[1], x[0]))

    def test_contains_without_analytics_walks_tree(self):
        tree = BKTree(config=BKTreeConfig(enable_analytics=False))
        tree.append(["alpha", "beta", "gamma"])
        assert tree.contains("beta")
        assert not tree.contains("alph")
        assert tree.stats is None

    def test_float_metric_query(self):
        tree = BKTree(distance_metric=JaccardDistance(),
                      config=BKTreeConfig(enable_analytics=False))
        tree.append(["abc", "abd", "xyz"])
        results = tree.query("abc", max_distance=0.5)
        assert ("abc", 0.0) in results
        assert all(d <= 0.5 for _, d in results)

    def test_thread_safe_parallel_inserts(self):
        tree = BKTree()
        words = [f"tool_{i}" for i in range(200)]

        def worker(chunk):
            for w in chunk:
                tree.add(w)

        threads = [threading.Thread(target=worker, args=(words[i::8],))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(tree) == 200
        assert all(w in tree for w in words)


class TestStats:
    def test_depth_stamped_on_add(self):
        tree = BKTree()
        tree.append(["hello", "helo", "world"])
        stats = tree.get_stats()
        assert stats.depths["hello"] == 0          # root
        assert stats.depths["helo"] == 1           # child at distance 1
        assert stats.depths["world"] >= 1

    def test_record_read_accumulates_depth(self):
        s = BKTreeStats()
        s.set_depth("hot", 3)
        s.record_read("hot")
        s.record_read("hot")
        assert s.read_weights["hot"] == 6
        s.record_read("unknown")  # default depth 1
        assert s.read_weights["unknown"] == 1

    def test_query_records_reads(self):
        tree = BKTree()
        tree.append(["read_file", "write_file"])
        tree.query("read_fiel", max_distance=2)
        weights = tree.get_stats().read_weights
        depth = tree.get_stats().depths["read_file"]
        assert weights["read_file"] == depth
        assert "write_file" not in weights  # not among hits

    def test_sorted_by_weight_descending(self):
        s = BKTreeStats()
        s.set_depth("a", 1)
        s.set_depth("b", 1)
        for _ in range(5):
            s.record_read("a")
        for _ in range(2):
            s.record_read("b")
        assert s.get_sorted_by_weight() == ["a", "b"]

    def test_quality_score(self):
        s = BKTreeStats()
        assert s.get_quality_score() == 0.0
        s.read_weights["x"] = 180
        s.read_weights["y"] = 10
        # mean/sum = 95/190 = 0.5 (documentation §7.1 example)
        assert s.get_quality_score() == 0.5

    def test_reset(self):
        s = BKTreeStats()
        s.set_depth("a", 2)
        s.record_read("a")
        s.needs_rebalance = True
        s.last_quality_score = 0.5
        s.reset()
        assert s.read_weights == {}
        assert s.depths == {}
        assert not s.needs_rebalance
        assert s.last_quality_score == 0.0

    def test_clear_stats_creates_new_object(self):
        tree = BKTree()
        tree.add("a")
        old = tree.get_stats()
        tree.clear_stats()
        assert tree.get_stats() is not old
        assert tree.get_stats().read_weights == {}

    def test_analytics_disabled_no_stats(self):
        tree = BKTree(config=BKTreeConfig(enable_analytics=False))
        tree.add("a")
        assert tree.get_stats() is None
        tree.clear_stats()  # must not raise
        assert tree.get_stats() is None


class TestRebalance:
    def make_skewed_tree(self):
        words = ["dominant"] + [f"w{i}_distinct_qwerty" for i in range(30)]
        tree = BKTree(config=BKTreeConfig(rebalance_check_interval=0))
        tree.append(words)
        return tree, words

    def test_manual_rebalance_moves_hot_word_to_root(self):
        tree, words = self.make_skewed_tree()
        stats = tree.get_stats()
        # simulate heavy usage of a deep word
        hot = stats.get_sorted_by_weight() or None
        target = words[-1]
        for _ in range(50):
            stats.record_read(target)
        before_depth = stats.depths[target]
        tree.rebalance()
        assert len(tree) == len(words)
        assert all(w in tree for w in words)
        assert tree.root.word == target
        assert tree.get_stats().depths[target] == 0
        assert tree.get_stats().depths[target] < before_depth

    def test_rebalance_keeps_never_read_words(self):
        tree = BKTree(config=BKTreeConfig(rebalance_check_interval=0))
        tree.append(["aaa", "bbb", "ccc"])
        tree.get_stats().record_read("bbb")
        tree.rebalance()
        assert len(tree) == 3
        assert sorted(tree._all_words) == ["aaa", "bbb", "ccc"]
        assert tree.root.word == "bbb"

    def test_quality_check_triggers_flag_and_callback(self):
        seen = []

        def callback(t, score):
            seen.append(score)

        tree = BKTree(config=BKTreeConfig(
            rebalance_check_interval=2,
            rebalance_quality_threshold=1.1,   # force trigger for any score
            on_need_rebalance=callback,
        ))
        tree.append(["read_file", "write_file"])
        tree.query("read_fiel", max_distance=2)
        assert not tree.needs_rebalance()      # interval not reached yet
        tree.query("read_fiel", max_distance=2)
        assert tree.needs_rebalance()
        assert len(seen) == 1
        assert 0.0 <= seen[0] <= 1.0
        assert tree.get_stats().last_quality_score == seen[0]

    def test_auto_rebalance_rebuilds(self):
        tree = BKTree(config=BKTreeConfig(
            rebalance_check_interval=1,
            rebalance_quality_threshold=1.1,
            auto_rebalance=True,
        ))
        tree.append(["read_file", "write_file"])
        before_root = tree.root.word
        tree.query("read_fiel", max_distance=2)  # triggers check → rebalance
        assert len(tree) == 2
        assert "read_file" in tree and "write_file" in tree
        assert tree.needs_rebalance() is False   # stats were reset
        assert before_root is not None

    def test_interval_zero_disables_checks(self):
        tree = BKTree(config=BKTreeConfig(rebalance_check_interval=0,
                                          rebalance_quality_threshold=1.1))
        tree.append(["a_x", "b_y"])
        for _ in range(10):
            tree.query("a_x", max_distance=2)
        assert not tree.needs_rebalance()

    def test_needs_rebalance_false_without_analytics(self):
        tree = BKTree(config=BKTreeConfig(enable_analytics=False))
        assert tree.needs_rebalance() is False


class TestFixTypo:
    TOOLS = ["read_file", "write_file", "delete_file", "list_directory"]

    def test_fix_typo_from_list(self):
        assert fix_typo("read_fiel", self.TOOLS) == "read_file"
        assert fix_typo("wriet_file", self.TOOLS) == "write_file"

    def test_fix_typo_exact_match(self):
        assert fix_typo("read_file", self.TOOLS) == "read_file"

    def test_fix_typo_no_match_returns_none(self):
        assert fix_typo("quantum_tunneling", self.TOOLS, max_distance=2) is None

    def test_fix_typo_with_ready_tree(self):
        tree = BKTree()
        tree.append(self.TOOLS)
        assert fix_typo("delet_file", tree, max_distance=2) == "delete_file"

    def test_fix_typo_picks_closest(self):
        candidates = ["read_file", "read_line"]
        assert fix_typo("read_lne", candidates, max_distance=2) == "read_line"


class TestLevenshteinFallback:
    def test_dp_fallback_matches_reference(self):
        # Force the pure-python DP path regardless of extras availability.
        import cascagent.bk_tree as bk_module

        pairs = [("kitten", "sitting"), ("", "abc"), ("abc", ""),
                 ("read_file", "read_fiel"), ("flaw", "lawn"),
                 ("гласные", "глосные")]
        try:
            real = bk_module._levenshtein_c
            bk_module._levenshtein_c = None
            for a, b in pairs:
                assert levenshtein_distance(a, b) == real(a, b) if real else True
                assert levenshtein_distance(a, b) == levenshtein_distance(b, a)
            assert levenshtein_distance("kitten", "sitting") == 3
            assert levenshtein_distance("read_file", "read_fiel") == 2
        finally:
            bk_module._levenshtein_c = real

    def test_tree_works_without_extras(self, monkeypatch):
        import cascagent.bk_tree as bk_module

        monkeypatch.setattr(bk_module, "_levenshtein_c", None)
        tree = BKTree()
        tree.append(["read_file", "write_file"])
        assert tree.query("read_fiel", max_distance=2) == [("read_file", 2)]


class TestNodeStructure:
    def test_children_keyed_by_distance(self):
        tree = BKTree()
        tree.append(["hello", "helo", "world"])
        assert isinstance(tree.root, BKNode)
        # d(hello,helo)=1, d(hello,world)=4 (one substitution + two deletions)
        assert set(tree.root.children) == {1, 4}
        assert tree.root.children[1].word == "helo"
        assert tree.root.children[4].word == "world"

    def test_same_distance_nests_deeper(self):
        tree = BKTree()
        tree.append(["hello", "helo", "hallo"])  # helo and hallo are both d=1
        assert set(tree.root.children) == {1}
        grandchild = tree.root.children[1]
        assert grandchild.word == "helo"
        assert grandchild.children[2].word == "hallo"  # d(hallo, helo) == 2

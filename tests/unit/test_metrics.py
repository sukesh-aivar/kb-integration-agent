"""Unit tests for evals.metrics (spec 7.5: purity/recall/silhouette/FP maths).

Pure, offline, no AWS. These lock the scoring maths before any pipeline exists,
so a later regression in a metric is caught immediately.
"""

from __future__ import annotations

import math

import pytest

from evals import metrics

# --- intent_purity -----------------------------------------------------------


def test_purity_perfect_clustering() -> None:
    # Two clusters, each pure wrt the true labels -> purity 1.0.
    pred = [0, 0, 1, 1]
    true = ["a", "a", "b", "b"]
    assert metrics.intent_purity(pred, true) == 1.0


def test_purity_worst_case_even_mix() -> None:
    # One cluster split 50/50 between two true labels -> majority is half.
    pred = [0, 0, 0, 0]
    true = ["a", "a", "b", "b"]
    assert metrics.intent_purity(pred, true) == 0.5


def test_purity_ignores_noise_bucket_by_default() -> None:
    # The -1 noise points must not be scored (they are unassigned).
    pred = [0, 0, -1, -1, -1]
    true = ["a", "a", "b", "c", "d"]
    # Only the two cluster-0 points are scored, and they are pure.
    assert metrics.intent_purity(pred, true) == 1.0


def test_purity_can_include_noise_when_asked() -> None:
    pred = [0, 0, -1, -1]
    true = ["a", "a", "b", "b"]
    # With ignore_label=None, the -1 group is its own pure cluster -> still 1.0.
    assert metrics.intent_purity(pred, true, ignore_label=None) == 1.0


def test_purity_all_noise_returns_zero() -> None:
    assert metrics.intent_purity([-1, -1], ["a", "b"]) == 0.0


def test_purity_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        metrics.intent_purity([0, 1], ["a"])


# --- noise_share / cluster_count ---------------------------------------------


def test_noise_share() -> None:
    assert metrics.noise_share([-1, -1, 0, 1]) == 0.5
    assert metrics.noise_share([]) == 0.0


def test_cluster_count_excludes_noise() -> None:
    assert metrics.cluster_count([-1, 0, 0, 1, 2]) == 3
    assert metrics.cluster_count([-1, -1]) == 0


# --- recall_at_k -------------------------------------------------------------


def test_recall_at_k_all_hits() -> None:
    retrieved = [["x", "y", "z"], ["a", "b"]]
    expected = ["y", "a"]
    assert metrics.recall_at_k(retrieved, expected, k=3) == 1.0


def test_recall_at_k_respects_cutoff() -> None:
    # Correct id sits at rank 3, so recall@2 misses it, recall@3 finds it.
    retrieved = [["x", "y", "target"]]
    expected = ["target"]
    assert metrics.recall_at_k(retrieved, expected, k=2) == 0.0
    assert metrics.recall_at_k(retrieved, expected, k=3) == 1.0


def test_recall_at_k_partial() -> None:
    retrieved = [["hit"], ["miss"]]
    expected = ["hit", "other"]
    assert metrics.recall_at_k(retrieved, expected, k=1) == 0.5


def test_recall_at_k_bad_k_raises() -> None:
    with pytest.raises(ValueError):
        metrics.recall_at_k([["a"]], ["a"], k=0)


def test_recall_at_k_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        metrics.recall_at_k([["a"]], ["a", "b"])


# --- false_positive_rate -----------------------------------------------------


def test_fp_rate_flag_everything_is_penalised() -> None:
    # 3 intents, all truly covered, all flagged as gaps -> FP rate 1.0.
    pred = ["gap", "gap", "gap"]
    truth = [True, True, True]
    assert metrics.false_positive_rate(pred, truth) == 1.0


def test_fp_rate_perfect_coverage_check() -> None:
    # Covered intents reported covered, real gap reported gap -> FP rate 0.0.
    pred = ["covered", "covered", "gap"]
    truth = [True, True, False]
    assert metrics.false_positive_rate(pred, truth) == 0.0


def test_fp_rate_partial_counts_as_flag() -> None:
    pred = ["partial", "covered"]
    truth = [True, True]
    assert metrics.false_positive_rate(pred, truth) == 0.5


def test_fp_rate_no_covered_intents_returns_zero() -> None:
    assert metrics.false_positive_rate(["gap"], [False]) == 0.0


# --- routing_accuracy --------------------------------------------------------


def test_routing_accuracy() -> None:
    assert metrics.routing_accuracy(["BDA", "DEFAULT"], ["BDA", "DEFAULT"]) == 1.0
    assert metrics.routing_accuracy(["BDA", "DEFAULT"], ["BDA", "BDA"]) == 0.5


def test_routing_accuracy_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        metrics.routing_accuracy(["BDA"], ["BDA", "DEFAULT"])


# --- silhouette (uses sklearn) -----------------------------------------------


def test_silhouette_two_separated_clusters_is_high() -> None:
    # Two tight, well-separated groups in 2D -> silhouette close to 1.
    embeddings = [[0.0, 0.0], [0.1, 0.0], [10.0, 10.0], [10.1, 10.0]]
    labels = [0, 0, 1, 1]
    score = metrics.silhouette(embeddings, labels, metric="euclidean")
    assert score > 0.8


def test_silhouette_undefined_returns_zero() -> None:
    # Fewer than 2 clusters -> undefined -> 0.0 by our contract.
    embeddings = [[0.0, 0.0], [1.0, 1.0]]
    assert metrics.silhouette(embeddings, [0, 0], metric="euclidean") == 0.0


def test_silhouette_is_finite() -> None:
    embeddings = [[0.0], [1.0], [8.0], [9.0]]
    score = metrics.silhouette(embeddings, [0, 0, 1, 1], metric="euclidean")
    assert math.isfinite(score)

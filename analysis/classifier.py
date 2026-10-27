"""ACTIVE/IDLE classifiers with cross-validation (FR-23, NFR-05).

Two models are trained on the 1-second windows: logistic regression and a random
forest.  Both are implemented here in plain Python so the evaluation pipeline
runs on a bare interpreter (no scikit-learn, no NumPy), which keeps the Windows
build self-contained and the results reproducible from the seeded RNG.

Reported metrics: accuracy, precision, recall and F1 for the ACTIVE class, with a
bootstrap 95% confidence interval for F1 (NFR-05).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from analysis.features import FEATURE_COLUMNS, FeatureWindow

ACTIVE = "active"
IDLE = "idle"


class ClassifierError(ValueError):
    """Not enough data, or a single class, so no classifier can be trained."""


@dataclass(frozen=True, slots=True)
class Metrics:
    accuracy: float
    precision: float
    recall: float
    f1: float
    support: int
    predicted_active: int
    true_active: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "accuracy": round(self.accuracy, 6),
            "precision": round(self.precision, 6),
            "recall": round(self.recall, 6),
            "f1": round(self.f1, 6),
            "support": self.support,
            "predicted_active": self.predicted_active,
            "true_active": self.true_active,
        }


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def metrics_for(y_true: Sequence[int], y_pred: Sequence[int]) -> Metrics:
    """Accuracy plus precision/recall/F1 for the ACTIVE (1) class."""
    if len(y_true) != len(y_pred):
        raise ClassifierError("label and prediction lengths differ")
    if not y_true:
        raise ClassifierError("no windows to score")
    tp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == 1 and p == 1)
    fp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == 1 and p == 0)
    tn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == 0 and p == 0)
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return Metrics(
        accuracy=_safe_div(tp + tn, len(y_true)),
        precision=precision,
        recall=recall,
        f1=f1,
        support=len(y_true),
        predicted_active=tp + fp,
        true_active=tp + fn,
    )


def stratified_folds(y: Sequence[int], folds: int, seed: int = 7) -> list[list[int]]:
    """Indices for k-fold cross-validation, preserving the class balance."""
    if folds < 2:
        raise ClassifierError("cross-validation needs at least two folds")
    rng = random.Random(seed)
    by_class: dict[int, list[int]] = {}
    for index, label in enumerate(y):
        by_class.setdefault(label, []).append(index)
    for indices in by_class.values():
        rng.shuffle(indices)
    buckets: list[list[int]] = [[] for _ in range(folds)]
    for indices in by_class.values():
        for position, index in enumerate(indices):
            buckets[position % folds].append(index)
    for bucket in buckets:
        bucket.sort()
    return buckets


class Standardiser:
    """Z-score scaling; constant features are left at zero."""

    def __init__(self) -> None:
        self.means: list[float] = []
        self.stds: list[float] = []

    def fit(self, rows: Sequence[Sequence[float]]) -> Standardiser:
        width = len(rows[0]) if rows else 0
        self.means = [0.0] * width
        self.stds = [1.0] * width
        for column in range(width):
            values = [row[column] for row in rows]
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / max(1, len(values) - 1)
            self.means[column] = mean
            self.stds[column] = math.sqrt(variance) or 1.0
        return self

    def transform(self, rows: Sequence[Sequence[float]]) -> list[list[float]]:
        return [
            [
                (value - self.means[column]) / self.stds[column]
                for column, value in enumerate(row)
            ]
            for row in rows
        ]

    def fit_transform(self, rows: Sequence[Sequence[float]]) -> list[list[float]]:
        return self.fit(rows).transform(rows)


class LogisticRegression:
    """L2-regularised logistic regression trained with gradient descent."""

    def __init__(self, *, iterations: int = 400, learning_rate: float = 0.25, l2: float = 0.01) -> None:
        self.iterations = iterations
        self.learning_rate = learning_rate
        self.l2 = l2
        self.weights: list[float] = []
        self.bias = 0.0

    def fit(self, rows: Sequence[Sequence[float]], labels: Sequence[int]) -> LogisticRegression:
        if not rows:
            raise ClassifierError("no training rows")
        width = len(rows[0])
        self.weights = [0.0] * width
        self.bias = 0.0
        n = len(rows)
        for _ in range(self.iterations):
            grad_w = [0.0] * width
            grad_b = 0.0
            for row, label in zip(rows, labels, strict=True):
                score = self.bias + sum(w * x for w, x in zip(self.weights, row, strict=True))
                probability = 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, score))))
                error = probability - label
                for index in range(width):
                    grad_w[index] += error * row[index]
                grad_b += error
            for index in range(width):
                self.weights[index] -= self.learning_rate * (
                    grad_w[index] / n + self.l2 * self.weights[index]
                )
            self.bias -= self.learning_rate * grad_b / n
        return self

    def decision_function(self, row: Sequence[float]) -> float:
        return self.bias + sum(w * x for w, x in zip(self.weights, row, strict=True))

    def predict_one(self, row: Sequence[float]) -> int:
        return 1 if self.decision_function(row) >= 0 else 0

    def predict(self, rows: Sequence[Sequence[float]]) -> list[int]:
        return [self.predict_one(row) for row in rows]


@dataclass(slots=True)
class _Node:
    feature: int = -1
    threshold: float = 0.0
    left: _Node | None = None
    right: _Node | None = None
    probability: float = 0.0
    samples: int = 0

    @property
    def is_leaf(self) -> bool:
        return self.feature < 0


class DecisionTree:
    """Small CART tree with Gini impurity and quantile candidate splits."""

    def __init__(
        self,
        *,
        max_depth: int = 6,
        min_samples: int = 8,
        max_features: int | None = None,
        seed: int = 7,
    ) -> None:
        self.max_depth = max_depth
        self.min_samples = min_samples
        self.max_features = max_features
        self.rng = random.Random(seed)
        self.root: _Node | None = None
        self.n_features = 0

    def fit(self, rows: Sequence[Sequence[float]], labels: Sequence[int]) -> DecisionTree:
        self.n_features = len(rows[0]) if rows else 0
        self.root = self._build(list(rows), list(labels), depth=0)
        return self

    def _build(self, rows: list[Sequence[float]], labels: list[int], depth: int) -> _Node:
        probability = sum(labels) / len(labels) if labels else 0.0
        node = _Node(probability=probability, samples=len(rows))
        if (
            depth >= self.max_depth
            or len(rows) < 2 * self.min_samples
            or len(set(labels)) <= 1
        ):
            return node

        features = list(range(self.n_features))
        if self.max_features is not None and self.max_features < len(features):
            features = self.rng.sample(features, self.max_features)

        best_gain = 0.0
        best_feature = -1
        best_threshold = 0.0
        parent_gini = _gini(labels)
        for feature in features:
            values = sorted({row[feature] for row in rows})
            if len(values) < 2:
                continue
            candidates = _quantiles(values, 12)
            for threshold in candidates:
                left_labels = [lab for row, lab in zip(rows, labels, strict=True) if row[feature] <= threshold]
                right_labels = [lab for row, lab in zip(rows, labels, strict=True) if row[feature] > threshold]
                if len(left_labels) < self.min_samples or len(right_labels) < self.min_samples:
                    continue
                weighted = (
                    len(left_labels) / len(labels) * _gini(left_labels)
                    + len(right_labels) / len(labels) * _gini(right_labels)
                )
                gain = parent_gini - weighted
                if gain > best_gain:
                    best_gain = gain
                    best_feature = feature
                    best_threshold = threshold
        if best_feature < 0:
            return node

        left_rows, left_labels, right_rows, right_labels = [], [], [], []
        for row, label in zip(rows, labels, strict=True):
            if row[best_feature] <= best_threshold:
                left_rows.append(row)
                left_labels.append(label)
            else:
                right_rows.append(row)
                right_labels.append(label)
        node.feature = best_feature
        node.threshold = best_threshold
        node.left = self._build(left_rows, left_labels, depth + 1)
        node.right = self._build(right_rows, right_labels, depth + 1)
        return node

    def predict_one(self, row: Sequence[float]) -> int:
        node = self.root
        if node is None:
            raise ClassifierError("tree is not fitted")
        while not node.is_leaf:
            node = node.left if row[node.feature] <= node.threshold else node.right
            if node is None:  # pragma: no cover - defensive
                return 0
        return 1 if node.probability >= 0.5 else 0

    def predict(self, rows: Sequence[Sequence[float]]) -> list[int]:
        return [self.predict_one(row) for row in rows]


class RandomForest:
    """Bagged decision trees with random feature subsets."""

    def __init__(
        self,
        *,
        n_trees: int = 25,
        max_depth: int = 7,
        min_samples: int = 6,
        seed: int = 7,
    ) -> None:
        self.n_trees = n_trees
        self.max_depth = max_depth
        self.min_samples = min_samples
        self.seed = seed
        self.trees: list[DecisionTree] = []

    def fit(self, rows: Sequence[Sequence[float]], labels: Sequence[int]) -> RandomForest:
        if not rows:
            raise ClassifierError("no training rows")
        rng = random.Random(self.seed)
        n_features = len(rows[0])
        mtry = max(1, int(math.sqrt(n_features)))
        self.trees = []
        for index in range(self.n_trees):
            sample_rows: list[Sequence[float]] = []
            sample_labels: list[int] = []
            for _ in range(len(rows)):
                pick = rng.randrange(len(rows))
                sample_rows.append(rows[pick])
                sample_labels.append(labels[pick])
            tree = DecisionTree(
                max_depth=self.max_depth,
                min_samples=self.min_samples,
                max_features=mtry,
                seed=self.seed + index * 31,
            )
            tree.fit(sample_rows, sample_labels)
            self.trees.append(tree)
        return self

    def predict(self, rows: Sequence[Sequence[float]]) -> list[int]:
        if not self.trees:
            raise ClassifierError("forest is not fitted")
        out: list[int] = []
        for row in rows:
            votes = sum(tree.predict_one(row) for tree in self.trees)
            out.append(1 if votes * 2 >= len(self.trees) else 0)
        return out


def _gini(labels: Sequence[int]) -> float:
    if not labels:
        return 0.0
    positive = sum(labels) / len(labels)
    return 2 * positive * (1 - positive)


def _quantiles(values: Sequence[float], count: int) -> list[float]:
    if len(values) <= count:
        return [values[i] for i in range(len(values) - 1)]
    step = (len(values) - 1) / count
    return [values[int(round(i * step))] for i in range(1, count)]


def cross_validate(
    factory: Callable[[], Any],
    rows: Sequence[Sequence[float]],
    labels: Sequence[int],
    *,
    folds: int = 5,
    seed: int = 7,
) -> tuple[Metrics, list[int]]:
    """Run stratified k-fold CV and return pooled out-of-fold metrics."""
    buckets = stratified_folds(labels, folds, seed=seed)
    predictions: list[int | None] = [None] * len(labels)
    for index in range(folds):
        test_indices = buckets[index]
        train_indices = [i for bucket_index, bucket in enumerate(buckets) if bucket_index != index for i in bucket]
        if not test_indices or not train_indices:
            continue
        train_rows = [rows[i] for i in train_indices]
        train_labels = [labels[i] for i in train_indices]
        if len(set(train_labels)) < 2:
            # Degenerate fold: predict the only class present.
            only = train_labels[0]
            for i in test_indices:
                predictions[i] = only
            continue
        standardiser = Standardiser().fit(train_rows)
        model = factory()
        model.fit(standardiser.transform(train_rows), train_labels)
        fold_predictions = model.predict(standardiser.transform([rows[i] for i in test_indices]))
        for i, prediction in zip(test_indices, fold_predictions, strict=True):
            predictions[i] = prediction
    filled = [int(p) if p is not None else 0 for p in predictions]
    return metrics_for(labels, filled), filled


def bootstrap_f1_ci(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    *,
    samples: int = 1000,
    seed: int = 7,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for F1 (NFR-05)."""
    if not y_true:
        return 0.0, 0.0
    rng = random.Random(seed)
    values: list[float] = []
    n = len(y_true)
    for _ in range(max(50, samples)):
        indices = [rng.randrange(n) for _ in range(n)]
        sample_true = [y_true[i] for i in indices]
        sample_pred = [y_pred[i] for i in indices]
        if len(set(sample_true)) < 1:
            continue
        values.append(metrics_for(sample_true, sample_pred).f1)
    if not values:
        return 0.0, 0.0
    values.sort()
    alpha = (1 - confidence) / 2
    low = values[int(alpha * (len(values) - 1))]
    high = values[int((1 - alpha) * (len(values) - 1))]
    return round(low, 6), round(high, 6)


def labelled_rows(windows: Sequence[FeatureWindow]) -> tuple[list[list[float]], list[int]]:
    """Feature matrix and 0/1 labels, dropping unlabelled windows."""
    rows: list[list[float]] = []
    labels: list[int] = []
    for window in windows:
        if window.label not in {ACTIVE, IDLE}:
            continue
        rows.append(window.vector())
        labels.append(1 if window.label == ACTIVE else 0)
    if len(set(labels)) < 2:
        raise ClassifierError("need both ACTIVE and IDLE windows to train a classifier")
    return rows, labels


@dataclass(slots=True)
class EvaluationResult:
    """Everything one evaluation run produces (FR-23, NFR-05)."""

    windows: int
    active_windows: int
    idle_windows: int
    folds: int
    lr: Metrics
    rf: Metrics
    lr_f1_ci: tuple[float, float]
    rf_f1_ci: tuple[float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows": self.windows,
            "active_windows": self.active_windows,
            "idle_windows": self.idle_windows,
            "folds": self.folds,
            "features": list(FEATURE_COLUMNS),
            "lr": {**self.lr.to_dict(), "f1_ci_low": self.lr_f1_ci[0], "f1_ci_high": self.lr_f1_ci[1]},
            "rf": {**self.rf.to_dict(), "f1_ci_low": self.rf_f1_ci[0], "f1_ci_high": self.rf_f1_ci[1]},
        }


def evaluate(
    windows: Sequence[FeatureWindow],
    *,
    folds: int = 5,
    seed: int = 7,
    bootstrap: int = 1000,
) -> EvaluationResult:
    """Train and score both classifiers with cross-validation and CIs."""
    rows, labels = labelled_rows(windows)
    folds = max(2, min(folds, min(sum(labels), len(labels) - sum(labels))))
    lr_metrics, lr_predictions = cross_validate(
        lambda: LogisticRegression(), rows, labels, folds=folds, seed=seed
    )
    rf_metrics, rf_predictions = cross_validate(
        lambda: RandomForest(seed=seed), rows, labels, folds=folds, seed=seed
    )
    return EvaluationResult(
        windows=len(windows),
        active_windows=sum(labels),
        idle_windows=len(labels) - sum(labels),
        folds=folds,
        lr=lr_metrics,
        rf=rf_metrics,
        lr_f1_ci=bootstrap_f1_ci(labels, lr_predictions, samples=bootstrap, seed=seed),
        rf_f1_ci=bootstrap_f1_ci(labels, rf_predictions, samples=bootstrap, seed=seed),
    )

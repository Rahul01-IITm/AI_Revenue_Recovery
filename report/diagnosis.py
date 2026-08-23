"""Diagnosis quality, measured against ground truth on the held-out split.

Evaluation is entitled to `GroundTruth`; the classifier is not. That asymmetry
is the only reason these numbers mean anything.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from pydantic import BaseModel, ConfigDict

from core.schemas import Batch, Diagnosis, Split
from core.taxonomy import FailureClass
from diagnose.classifier import classify_all


class ClassScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    failure_class: FailureClass
    support: int
    correct: int
    predicted: int

    @property
    def recall(self) -> float:
        return self.correct / self.support if self.support else 0.0

    @property
    def precision(self) -> float:
        return self.correct / self.predicted if self.predicted else 0.0


class DiagnosisReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    split: Split | None
    total: int
    classified: int
    correct: int

    by_class: list[ClassScore]
    confusions: list[tuple[str, str, int]]
    """(true, predicted, count), worst first."""
    accuracy_by_confidence: dict[str, tuple[int, int]]
    """confidence band -> (correct, total)."""

    @property
    def accuracy(self) -> float:
        """Over all rows, counting unclassified as wrong. The honest denominator."""
        return self.correct / self.total if self.total else 0.0

    @property
    def accuracy_when_classified(self) -> float:
        return self.correct / self.classified if self.classified else 0.0

    @property
    def coverage(self) -> float:
        return self.classified / self.total if self.total else 0.0


def evaluate(batch: Batch, split: Split | None = "test") -> DiagnosisReport:
    txns = batch.select(split)
    diagnoses = classify_all(txns)

    correct = 0
    classified = 0
    support: Counter = Counter()
    predicted: Counter = Counter()
    hits: Counter = Counter()
    confusion: Counter = Counter()
    bands: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for t in txns:
        truth = batch.true_class(t.txn_id)
        d: Diagnosis = diagnoses[t.txn_id]
        support[truth] += 1

        band = _band(d.confidence)
        bands[band][1] += 1

        if not d.is_classified:
            confusion[(truth.value, "UNCLASSIFIED")] += 1
            continue

        classified += 1
        predicted[d.failure_class] += 1
        if d.failure_class == truth:
            correct += 1
            hits[truth] += 1
            bands[band][0] += 1
        else:
            confusion[(truth.value, d.failure_class.value)] += 1

    by_class = [
        ClassScore(
            failure_class=cls,
            support=support[cls],
            correct=hits[cls],
            predicted=predicted[cls],
        )
        for cls in sorted(support, key=lambda c: -support[c])
    ]

    return DiagnosisReport(
        split=split,
        total=len(txns),
        classified=classified,
        correct=correct,
        by_class=by_class,
        confusions=[(a, b, n) for (a, b), n in confusion.most_common()],
        accuracy_by_confidence={k: (v[0], v[1]) for k, v in sorted(bands.items())},
    )


def _band(confidence: float) -> str:
    if confidence >= 0.90:
        return "0.90+  (unambiguous code)"
    if confidence >= 0.80:
        return "0.80+  (ambiguous, resolved)"
    if confidence >= 0.50:
        return "0.50+  (ambiguous, defaulted)"
    if confidence > 0.0:
        return "0.01+  (message only)"
    return "0.00   (unclassified)"


def render(report: DiagnosisReport) -> str:
    lines = [
        "",
        f"  Split                {report.split or 'all'}",
        f"  Transactions         {report.total}",
        "",
        f"  Accuracy (overall)   {report.accuracy:.1%}"
        f"   <- unclassified counted as wrong",
        f"  Accuracy (of those classified)  {report.accuracy_when_classified:.1%}",
        f"  Coverage             {report.coverage:.1%}"
        f"   ({report.classified}/{report.total} classified)",
        "",
        "  Calibration - accuracy should fall as confidence falls:",
        "",
    ]
    for band, (ok, n) in report.accuracy_by_confidence.items():
        rate = ok / n if n else 0.0
        lines.append(f"    {band:<30}{ok:>4}/{n:<5}{rate:>7.1%}")

    lines += ["", "  By failure class:", ""]
    header = (
        f"    {'class':<22}{'support':>8}{'recall':>9}{'precision':>11}"
    )
    lines += [header, "    " + "-" * (len(header) - 4)]
    for c in report.by_class:
        lines.append(
            f"    {c.failure_class:<22}{c.support:>8}"
            f"{c.recall:>9.1%}{c.precision:>11.1%}"
        )

    if report.confusions:
        lines += ["", "  Misclassifications (true -> predicted):", ""]
        for true, pred, n in report.confusions[:8]:
            lines.append(f"    {true:<22} -> {pred:<22} {n:>4}")
    lines.append("")
    return "\n".join(lines)

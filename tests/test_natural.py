"""Simulator Column A: the constants are a spec, not a tuning knob.

The first test here is the important one. It parses SIMULATION_ASSUMPTIONS.md
and asserts the frozen table matches the code, so nobody can quietly nudge a
probability to make a slide read better without the suite going red.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from data.generator import generate_batch
from core.taxonomy import FailureClass
from simulate.natural import (
    P_NATURAL_RECOVERY,
    expected_natural_recovery,
    recovers_naturally,
)

ASSUMPTIONS = Path(__file__).parent.parent / "simulate" / "SIMULATION_ASSUMPTIONS.md"


def section(heading: str) -> str:
    """Text of one `## ` section, exclusive of the next.

    Scoping matters: Column B rows are `| `CLASS` | `ACTION` | **0.55** |`, so an
    unscoped Column A pattern happily matches the *action* token and silently
    reads the wrong table.
    """
    text = ASSUMPTIONS.read_text()
    start = text.index(heading)
    rest = text[start + len(heading) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _parse_column_a() -> dict[str, float]:
    """Pull `| `CLASS` | **0.30** |` rows out of the frozen Column A table."""
    pattern = re.compile(r"\|\s*`([A-Z0-9_]+)`\s*\|\s*\*\*([0-9.]+)\*\*\s*\|")
    return {
        m.group(1): float(m.group(2))
        for m in pattern.finditer(section("## Column A"))
    }


def test_code_matches_the_frozen_assumptions_document():
    documented = _parse_column_a()
    assert documented, "could not parse Column A out of SIMULATION_ASSUMPTIONS.md"

    in_code = {cls.value: p for cls, p in P_NATURAL_RECOVERY.items()}
    assert documented == in_code, (
        "SIMULATION_ASSUMPTIONS.md and simulate/natural.py disagree. "
        "The document is the spec: change both together and note it in the "
        "change log, or change neither."
    )


def test_every_failure_class_has_a_probability():
    for cls in FailureClass:
        assert cls in P_NATURAL_RECOVERY


def test_terminal_classes_never_recover():
    assert P_NATURAL_RECOVERY[FailureClass.SUSPECTED_FRAUD] == 0.0
    assert P_NATURAL_RECOVERY[FailureClass.INVALID_ACCOUNT] == 0.0


def test_probabilities_are_valid():
    for cls, p in P_NATURAL_RECOVERY.items():
        assert 0.0 <= p <= 1.0, cls


# --- Draw mechanics ----------------------------------------------------------


@pytest.fixture(scope="module")
def batch():
    return generate_batch(n=500, seed=42)


def test_draws_are_paired_and_order_independent(batch):
    """The same transaction must get the same draw regardless of when it is
    evaluated. This is what makes baseline and agent comparable on identical
    luck rather than on sampling noise."""
    txns = batch.transactions
    forward = {
        t.txn_id: recovers_naturally(t, batch.true_class(t.txn_id), seed=batch.seed)
        for t in txns
    }
    backward = {
        t.txn_id: recovers_naturally(t, batch.true_class(t.txn_id), seed=batch.seed)
        for t in reversed(txns)
    }
    assert forward == backward


def test_draws_survive_filtering_to_a_split(batch):
    """Evaluating only the test split must not change any test-split outcome."""
    full = {
        t.txn_id: recovers_naturally(t, batch.true_class(t.txn_id), seed=batch.seed)
        for t in batch.transactions
    }
    for t in batch.select("test"):
        assert (
            recovers_naturally(t, batch.true_class(t.txn_id), seed=batch.seed)
            == full[t.txn_id]
        )


def test_draws_are_stable_across_processes(batch):
    """Guards against `hash()`, which is salted per interpreter run."""
    import subprocess
    import sys

    code = (
        "from data.generator import generate_batch;"
        "from simulate.natural import recovers_naturally;"
        "b = generate_batch(n=50, seed=42);"
        "print(sum(recovers_naturally(t, b.true_class(t.txn_id), seed=b.seed)"
        " for t in b.transactions))"
    )
    root = Path(__file__).parent.parent
    runs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, cwd=root
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(runs) == 1, f"unstable across processes: {runs}"


def test_seed_changes_the_draws(batch):
    a = sum(
        recovers_naturally(t, batch.true_class(t.txn_id), seed=42)
        for t in batch.transactions
    )
    b = sum(
        recovers_naturally(t, batch.true_class(t.txn_id), seed=99)
        for t in batch.transactions
    )
    assert a != b


def test_sampled_recovery_is_near_its_expectation(batch):
    """A broken RNG should fail here rather than produce a plausible number."""
    truth = {t.txn_id: batch.true_class(t.txn_id) for t in batch.transactions}
    expected = expected_natural_recovery(batch.transactions, truth)
    actual = sum(
        (
            t.amount
            for t in batch.transactions
            if recovers_naturally(t, truth[t.txn_id], seed=batch.seed)
        ),
        Decimal("0"),
    )
    # Wide band: 500 rows with planted high-value outliers is a noisy sample.
    assert Decimal("0.4") * expected < actual < Decimal("2.0") * expected

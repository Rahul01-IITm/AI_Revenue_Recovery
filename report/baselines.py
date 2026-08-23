"""Baselines. Built before any intelligence, so the floor is fixed early.

`do_nothing` is the only mode implemented in step 1. `naive_retry_all` needs the
executor and the per-action simulator columns, so it lands with step 5 — the
signature is declared here to keep the shape of the comparison visible.
"""

from __future__ import annotations

from decimal import Decimal

from core.schemas import Batch, Split
from report.metrics import RunResult, build_breakdown
from simulate.natural import recovers_naturally


def do_nothing(batch: Batch, split: Split | None = "test") -> RunResult:
    """The floor: take no action and count what recovers by itself.

    Every number the agent later claims is measured against this. It reads
    ground truth only through the simulator, which is the one component entitled
    to it.
    """
    txns = batch.select(split)

    rows = []
    recovered_count = 0
    recovered_amount = Decimal("0")
    at_risk = Decimal("0")

    for t in txns:
        cls = batch.true_class(t.txn_id)
        recovered = recovers_naturally(t, cls, seed=batch.seed)
        at_risk += t.amount
        if recovered:
            recovered_count += 1
            recovered_amount += t.amount
        rows.append((cls, t.amount, recovered))

    return RunResult(
        mode="do-nothing",
        split=split,
        seed=batch.seed,
        count=len(txns),
        at_risk=at_risk,
        recovered_count=recovered_count,
        recovered_amount=recovered_amount,
        by_class=build_breakdown(rows),
        intervention_cost=Decimal("0"),
    )


def naive_retry_all(batch: Batch, split: Split | None = "test") -> RunResult:
    """Retry everything twice, immediately, and message everyone.

    The honest competitor — this is what most merchants actually do. Beating it
    is the real claim, and it also demonstrates that naive retry contacts
    opted-out customers and retries fraud.

    Implemented in `runner.run_naive`; re-exported here so both baselines are
    reachable from one module.
    """
    from runner import run_naive

    return run_naive(batch, split=split)

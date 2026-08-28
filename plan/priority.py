"""Which transactions to work first, when capacity is finite.

Recoverability answers *how likely* a transaction is to come back. Priority
answers *how much it is worth trying* — a different question, and deliberately
a separate module. Folding value into the recoverability score would corrupt
it: a ₹24,000 transaction is not more *likely* to recover than a ₹499 one, it
is merely worth more. Mixing the two would make `RecoverabilityAssessment.score`
mean neither thing cleanly.

Priority only matters where a resource is scarce. Retries and messages are
cheap and capped per transaction, so the agent can afford to work every row
that policy permits. Human review is neither — `MAX_HUMAN_ESCALATIONS_PER_RUN`
binds, so the order in which escalations are offered determines which ones
actually get a person.
"""

from __future__ import annotations

from decimal import Decimal

from core.schemas import RecoverabilityAssessment, Transaction

#: Weight on lifetime value when estimating what a failed payment puts at risk.
#:
#: A failed subscription payment risks more than the payment: some fraction of
#: customers churn rather than fix it, taking their remaining lifetime value
#: with them. 0.15 is a deliberately conservative stand-in for that churn risk
#: — high enough that a high-LTV customer outranks an equal-value one-off,
#: low enough that LTV never dominates the amount actually at stake.
#:
#: This is an assumption, not a measurement. It is stated here rather than
#: buried in a formula so it can be argued with.
LTV_CHURN_WEIGHT = Decimal("0.15")


def value_at_stake(txn: Transaction) -> Decimal:
    """Rupees genuinely at risk: the payment, plus the relationship behind it."""
    return txn.amount + LTV_CHURN_WEIGHT * txn.customer_ltv


def escalation_priority(
    txn: Transaction, assessment: RecoverabilityAssessment
) -> Decimal:
    """Expected recoverable value. Higher is worked first.

    The product of what is at stake and how winnable it looks, so the scarce
    human budget is not spent on a large transaction nobody can save or on a
    highly recoverable one worth ₹149.
    """
    return value_at_stake(txn) * Decimal(str(assessment.score))


def order_queue(
    scored: list[tuple[Transaction, RecoverabilityAssessment]],
) -> list[tuple[Transaction, RecoverabilityAssessment]]:
    """Sort a batch into the order it should be worked.

    Safe to apply because every outcome draw is keyed by `txn_id` rather than
    pulled from a sequential stream (see SIMULATION_ASSUMPTIONS.md, "Draw order
    is irrelevant"). Reordering the queue changes which transactions get scarce
    capacity; it cannot change any transaction's luck.

    Ties break on `txn_id` so the order is deterministic.
    """
    return sorted(
        scored,
        key=lambda pair: (-escalation_priority(*pair), pair[0].txn_id),
    )

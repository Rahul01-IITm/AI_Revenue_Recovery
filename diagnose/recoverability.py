"""How winnable is this transaction? A signal for the planner, not a decision.

Every constant here is stated and every adjustment is recorded in
`RecoverabilityAssessment.factors`, so a judge asking "why did it score 0.31?"
gets an answer from the audit trail rather than from a debugger.

Deliberately *not* a model. Step 2 of the build order says rules, no ML. A
logistic regression here would be marginally better calibrated and considerably
harder to defend on screen.
"""

from __future__ import annotations

from core.schemas import Diagnosis, RecoverabilityAssessment, Transaction
from core.taxonomy import RECOVERABILITY, FailureClass, Recoverability

#: Starting point per recoverability tier, before per-transaction signals.
TIER_BASE: dict[Recoverability, float] = {
    Recoverability.HIGH: 0.70,
    Recoverability.MEDIUM: 0.45,
    Recoverability.LOW: 0.20,
    Recoverability.NEVER: 0.00,
}

#: A customer who nearly always pays is a better bet than one who rarely does.
#: Centred on 0.80, which is roughly the population mean.
PSR_CENTRE = 0.80
PSR_WEIGHT = 0.25

#: Each prior retry is evidence the easy win already failed.
RETRY_PENALTY = 0.10

#: Repeated recent failures suggest a customer-side problem, not a blip.
FAILURE_7D_PENALTY = 0.05
FAILURE_7D_CAP = 4

#: Classes whose recovery *requires* reaching the customer. If we cannot contact
#: them, the realistic ceiling drops sharply.
NEEDS_CONTACT: frozenset[FailureClass] = frozenset(
    {
        FailureClass.CARD_EXPIRED,
        FailureClass.MANDATE_EXPIRED,
        FailureClass.MANDATE_REVOKED,
        FailureClass.CHECKOUT_ABANDONED,
        FailureClass.THREE_DS_TIMEOUT,
    }
)
UNCONTACTABLE_PENALTY = 0.30


def assess(txn: Transaction, diagnosis: Diagnosis) -> RecoverabilityAssessment:
    """Score in [0, 1]. Hard zeros are hard: they cannot be argued back up."""
    factors: list[str] = []

    if not diagnosis.is_classified:
        return RecoverabilityAssessment(
            txn_id=txn.txn_id,
            score=0.0,
            tier=Recoverability.NEVER,
            confidence=diagnosis.confidence,
            factors=("undiagnosed: no rule matched, cannot assess recoverability",),
        )

    cls = diagnosis.failure_class
    tier = RECOVERABILITY[cls]

    # --- Hard zeros ---------------------------------------------------------
    if txn.chargeback_open:
        return RecoverabilityAssessment(
            txn_id=txn.txn_id,
            score=0.0,
            tier=Recoverability.NEVER,
            confidence=diagnosis.confidence,
            factors=("chargeback open: never recoverable, never retryable",),
        )

    if tier is Recoverability.NEVER:
        return RecoverabilityAssessment(
            txn_id=txn.txn_id,
            score=0.0,
            tier=tier,
            confidence=diagnosis.confidence,
            factors=(f"{cls.value} is terminal: recovery is not attempted",),
        )

    # --- Graded signals -----------------------------------------------------
    score = TIER_BASE[tier]
    factors.append(f"base {score:.2f} from {cls.value} tier {tier.value}")

    psr_delta = (txn.prior_success_rate - PSR_CENTRE) * PSR_WEIGHT
    if abs(psr_delta) >= 0.005:
        score += psr_delta
        factors.append(
            f"{psr_delta:+.3f} prior success rate {txn.prior_success_rate:.2f}"
        )

    if txn.retry_count:
        delta = -RETRY_PENALTY * txn.retry_count
        score += delta
        factors.append(f"{delta:+.2f} for {txn.retry_count} prior retries")

    if txn.prior_failures_7d:
        n = min(txn.prior_failures_7d, FAILURE_7D_CAP)
        delta = -FAILURE_7D_PENALTY * n
        score += delta
        factors.append(f"{delta:+.2f} for {txn.prior_failures_7d} failures in 7d")

    if cls in NEEDS_CONTACT and not txn.is_contactable:
        score -= UNCONTACTABLE_PENALTY
        reason = "opted out" if txn.opted_out else "no channel preferences"
        factors.append(
            f"-{UNCONTACTABLE_PENALTY:.2f} {cls.value} needs contact but "
            f"customer is unreachable ({reason})"
        )

    score = max(0.0, min(1.0, score))
    return RecoverabilityAssessment(
        txn_id=txn.txn_id,
        score=round(score, 4),
        tier=tier,
        confidence=diagnosis.confidence,
        factors=tuple(factors),
    )


def assess_all(
    txns: list[Transaction], diagnoses: dict[str, Diagnosis]
) -> dict[str, RecoverabilityAssessment]:
    return {t.txn_id: assess(t, diagnoses[t.txn_id]) for t in txns}

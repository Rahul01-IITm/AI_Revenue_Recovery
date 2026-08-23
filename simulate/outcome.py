"""Column B of the simulator: what an intervention adds.

Transcribed from the frozen `SIMULATION_ASSUMPTIONS.md`. `tests/test_outcome.py`
parses that document and fails if the two disagree, so a probability cannot be
nudged to flatter a slide without the suite going red.

Lift *composes* with the natural rate rather than replacing it:

    P(recover | action) = P_natural + (1 - P_natural) x LIFT

so a transaction that would have recovered anyway is flagged `counterfactual`
and excluded from uplift. The agent is never credited for luck.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from core.schemas import Transaction
from core.taxonomy import Action, FailureClass
from simulate.natural import P_NATURAL_RECOVERY, _uniform

C = FailureClass
A = Action

#: Incremental lift per (failure class, action). Absent pairs are 0.00 --
#: silence is not permission.
LIFT: dict[FailureClass, dict[Action, float]] = {
    C.ISSUER_DOWN: {A.RETRY: 0.55, A.ESCALATE_HUMAN: 0.40},
    C.INSUFFICIENT_FUNDS: {
        A.RETRY_SALARY_ALIGNED: 0.45,
        A.RETRY: 0.08,
        A.NUDGE_WHATSAPP: 0.15,
        A.NUDGE_EMAIL: 0.10,
        A.ESCALATE_HUMAN: 0.30,
    },
    C.CARD_EXPIRED: {
        A.REQUEST_INSTRUMENT_UPDATE: 0.35,
        A.SEND_PAYMENT_LINK: 0.25,
        A.RETRY: 0.00,
        A.ESCALATE_HUMAN: 0.30,
    },
    C.MANDATE_EXPIRED: {
        A.REQUEST_MANDATE_RENEWAL: 0.30,
        A.SEND_PAYMENT_LINK: 0.20,
        A.RETRY: 0.00,
        A.ESCALATE_HUMAN: 0.25,
    },
    C.MANDATE_REVOKED: {
        A.REQUEST_MANDATE_RENEWAL: 0.12,
        A.SEND_PAYMENT_LINK: 0.15,
        A.RETRY: 0.00,
        A.ESCALATE_HUMAN: 0.20,
    },
    C.THREE_DS_TIMEOUT: {
        A.SEND_PAYMENT_LINK: 0.50,
        A.NUDGE_WHATSAPP: 0.25,
        A.NUDGE_EMAIL: 0.18,
        A.RETRY: 0.12,
        A.ESCALATE_HUMAN: 0.35,
    },
    C.CHECKOUT_ABANDONED: {
        A.SEND_PAYMENT_LINK: 0.30,
        A.NUDGE_WHATSAPP: 0.25,
        A.NUDGE_EMAIL: 0.15,
        A.RETRY: 0.02,
        A.ESCALATE_HUMAN: 0.25,
    },
    C.DO_NOT_HONOUR: {
        A.RETRY: 0.15,
        A.SEND_PAYMENT_LINK: 0.18,
        A.ESCALATE_HUMAN: 0.20,
    },
    C.SUSPECTED_FRAUD: {},
    C.INVALID_ACCOUNT: {},
    C.B2B_INVOICE_OVERDUE: {},
}

HUMAN_REVIEW_COST = Decimal("150.00")
"""Roughly 15 minutes of an operations person. Escalation is not free, and
pretending otherwise would make the Rs.50k gate look costless."""


def timing_multiplier(
    cls: FailureClass, action: Action, scheduled_at: datetime, failed_at: datetime
) -> float:
    """Same action, wrong moment, less money."""
    delay = scheduled_at - failed_at

    if cls is C.ISSUER_DOWN and action is A.RETRY and delay < timedelta(hours=1):
        # The outage probably has not cleared. This is what naive-retry-all does.
        return 0.40
    if cls is C.THREE_DS_TIMEOUT and action is A.SEND_PAYMENT_LINK:
        return 1.00 if delay <= timedelta(hours=2) else 0.60
    if cls is C.CHECKOUT_ABANDONED and delay > timedelta(hours=4):
        return 0.70
    return 1.00


def recovery_probability(
    cls: FailureClass, action: Action, scheduled_at: datetime, failed_at: datetime
) -> float:
    """Total P(recovery) for a transaction receiving this action."""
    natural = P_NATURAL_RECOVERY[cls]
    lift = LIFT.get(cls, {}).get(action, 0.0)
    lift *= timing_multiplier(cls, action, scheduled_at, failed_at)
    return natural + (1.0 - natural) * lift


def attempt_succeeds(
    txn: Transaction,
    cls: FailureClass,
    action: Action,
    scheduled_at: datetime,
    *,
    seed: int,
    attempt_no: int,
) -> bool:
    """Did this specific intervention recover the money?

    Draw is keyed by `(seed, txn_id, action, attempt)` so it is stable across
    runs and independent of evaluation order -- the same pairing property that
    makes the baseline comparison fair.
    """
    p = recovery_probability(cls, action, scheduled_at, txn.timestamp)
    stream = f"attempt:{action.value}:{attempt_no}"
    return _uniform(seed, txn.txn_id, stream) < p


def action_cost(action: Action, message_cost: Decimal) -> Decimal:
    """Total cost of one action, including human time for escalations."""
    if action is A.ESCALATE_HUMAN:
        return HUMAN_REVIEW_COST
    return message_cost

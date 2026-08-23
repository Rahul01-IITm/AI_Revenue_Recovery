"""Chooses an action and a time from the allowed set. Proposes only.

The planner cannot execute anything and cannot exceed the `Action` enum. Its
output goes to the policy engine, which may veto, downgrade, or defer it.

Timing is transcribed from CLAUDE.md's taxonomy table. It matters as much as the
action does: retrying an `INSUFFICIENT_FUNDS` transaction immediately burns
issuer trust for no gain, which is why that class waits for money to arrive
rather than retrying on the spot.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core.schemas import (
    Diagnosis,
    PlannedAction,
    RecoverabilityAssessment,
    Transaction,
)
from core.taxonomy import Action, Channel, FailureClass
from policy.limits import IST, RECOVERY_WINDOW

#: Latest a follow-up may be scheduled and still clear the 72h window.
#: One hour of headroom so a deferral (quiet hours) does not push it over.
LAST_PRACTICAL_SLOT = RECOVERY_WINDOW - timedelta(hours=1)

#: Below this recoverability score, chasing costs more than it returns.
#: A stopping rule the agent applies to itself before policy ever sees it.
MIN_SCORE_TO_ACT = 0.15


def _nudge_action(txn: Transaction) -> Action:
    """WhatsApp if the customer accepts it, else email. SMS is not a nudge
    channel here — it is reserved for payment links, where brevity helps."""
    if Channel.WHATSAPP in txn.channel_prefs:
        return Action.NUDGE_WHATSAPP
    return Action.NUDGE_EMAIL


def _next_salary_window(txn: Transaction, now: datetime) -> datetime:
    """When money is next likely to be in the account.

    Real tension worth naming: the 72h recovery window and true salary
    alignment are in conflict for most customers, because payday is usually
    further away than three days. We align when payday genuinely falls inside
    the window and otherwise take the last practical slot, which is the spec's
    stated T+72h fallback. Extending the window for this class is a policy
    change and belongs in CLAUDE.md, not in a quiet edit here.
    """
    deadline = txn.timestamp + LAST_PRACTICAL_SLOT
    if txn.salary_day is None:
        return deadline

    local = now.astimezone(IST)
    candidate = local.replace(
        day=min(txn.salary_day, 28), hour=10, minute=0, second=0, microsecond=0
    )
    if candidate <= local:
        month = candidate.month % 12 + 1
        year = candidate.year + (1 if month == 1 else 0)
        candidate = candidate.replace(year=year, month=month)

    aligned = candidate.astimezone(now.tzinfo)
    return aligned if aligned <= deadline else deadline


def plan(
    txn: Transaction,
    diagnosis: Diagnosis,
    assessment: RecoverabilityAssessment,
    now: datetime,
    attempt_no: int = 1,
) -> PlannedAction | None:
    """Propose the next intervention, or `None` when there is nothing to do.

    `None` and `STOP` mean different things: `None` is "the ladder is finished",
    `STOP` is "an active decision to give up", and both are logged distinctly.
    """
    cls = diagnosis.failure_class

    if cls is None:
        # The policy engine turns this into ESCALATE_HUMAN. The planner does not
        # pre-empt that ruling; it just declines to invent an action.
        return PlannedAction(
            txn_id=txn.txn_id,
            action=Action.ESCALATE_HUMAN,
            scheduled_at=now,
            rationale=f"Undiagnosed ({diagnosis.rule_id}); needs a human.",
            attempt_no=attempt_no,
        )

    if assessment.score < MIN_SCORE_TO_ACT:
        return PlannedAction(
            txn_id=txn.txn_id,
            action=Action.STOP,
            scheduled_at=now,
            rationale=(
                f"Recoverability {assessment.score:.2f} below the "
                f"{MIN_SCORE_TO_ACT:.2f} floor; not worth chasing."
            ),
            attempt_no=attempt_no,
        )

    action, delay = _ladder_rung(txn, cls, attempt_no)
    if action is None:
        return None

    if action is Action.RETRY_SALARY_ALIGNED:
        scheduled_at = _next_salary_window(txn, now)
        rationale = (
            f"{cls.value}: waiting for funds. "
            + (
                f"Customer's payday is day {txn.salary_day}."
                if txn.salary_day
                else "Payday unknown, using the T+72h fallback."
            )
        )
    else:
        scheduled_at = txn.timestamp + delay
        rationale = (
            f"{cls.value} (confidence {diagnosis.confidence:.2f}, "
            f"recoverability {assessment.score:.2f}): "
            f"{action.value} at T+{delay.total_seconds() / 3600:.1f}h."
        )

    return PlannedAction(
        txn_id=txn.txn_id,
        action=action,
        scheduled_at=max(scheduled_at, now),
        rationale=rationale,
        attempt_no=attempt_no,
    )


def _ladder_rung(
    txn: Transaction, cls: FailureClass, attempt_no: int
) -> tuple[Action | None, timedelta]:
    """The (action, delay) for this rung, or `(None, _)` when the ladder ends.

    Distinct failure classes must produce visibly different behaviour here, or
    the agent is a retry loop with extra steps.
    """
    zero = timedelta(0)

    if cls is FailureClass.ISSUER_DOWN:
        # Pure infrastructure. Retry is close to free money.
        return {1: (Action.RETRY, timedelta(hours=2)),
                2: (Action.RETRY, timedelta(hours=6))}.get(attempt_no, (None, zero))

    if cls is FailureClass.INSUFFICIENT_FUNDS:
        # Never retry immediately: the balance has not changed and a decline
        # costs issuer trust.
        if attempt_no == 1:
            return Action.RETRY_SALARY_ALIGNED, zero
        if attempt_no == 2:
            return _nudge_action(txn), timedelta(hours=48)
        return None, zero

    if cls is FailureClass.CARD_EXPIRED:
        # Retry is guaranteed to fail; only the customer can fix this.
        if attempt_no == 1:
            return Action.REQUEST_INSTRUMENT_UPDATE, zero
        if attempt_no == 2:
            return Action.SEND_PAYMENT_LINK, timedelta(hours=24)
        return None, zero

    if cls in (FailureClass.MANDATE_EXPIRED, FailureClass.MANDATE_REVOKED):
        if attempt_no == 1:
            return Action.REQUEST_MANDATE_RENEWAL, zero
        if attempt_no == 2:
            return Action.SEND_PAYMENT_LINK, timedelta(hours=24)
        return None, zero

    if cls is FailureClass.THREE_DS_TIMEOUT:
        # Intent was present; this is a warm lead, so move fast.
        if attempt_no == 1:
            return Action.SEND_PAYMENT_LINK, timedelta(minutes=30)
        if attempt_no == 2:
            return _nudge_action(txn), timedelta(hours=24)
        return None, zero

    if cls is FailureClass.CHECKOUT_ABANDONED:
        if attempt_no == 1:
            return _nudge_action(txn), timedelta(hours=1)
        if attempt_no == 2:
            return Action.SEND_PAYMENT_LINK, timedelta(hours=24)
        return None, zero

    if cls is FailureClass.DO_NOT_HONOUR:
        # Ambiguous issuer response: one retry, then stop. The enum has no
        # SINGLE_RETRY, so the ladder simply ends after rung 1.
        if attempt_no == 1:
            return Action.RETRY, timedelta(hours=24)
        return None, zero

    if cls in (FailureClass.SUSPECTED_FRAUD, FailureClass.INVALID_ACCOUNT):
        return (Action.STOP, zero) if attempt_no == 1 else (None, zero)

    return None, zero

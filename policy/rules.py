"""The individual guardrails.

Each rule is a pure function of `(txn, planned, state, now)` returning either a
`Ruling` or `None` (meaning "no objection"). Keeping them independent and
side-effect free is what lets `tests/test_policy.py` prove each one fires on its
own, and what makes the engine readable if a judge asks to see it on screen.

Rules are evaluated in the order given by `ORDERED_RULES`. The first VETO wins;
DEFER and DOWNGRADE accumulate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.schemas import PlannedAction, Transaction
from core.taxonomy import (
    NEVER_RETRY,
    OUTBOUND_ACTIONS,
    RETRY_ACTIONS,
    Action,
    FailureClass,
    Verdict,
)
from policy.limits import (
    HUMAN_APPROVAL_THRESHOLD,
    MAX_CUSTOMER_MESSAGES_PER_TXN,
    MAX_MESSAGES_PER_CUSTOMER_7D,
    MAX_RETRIES_PER_TXN,
    RECOVERY_WINDOW,
    is_quiet_hours,
    next_allowed_contact_time,
)
from policy.state import RunState


@dataclass(frozen=True)
class Ruling:
    verdict: Verdict
    rule_id: str
    reason: str
    replacement_action: Action | None = None
    replacement_time: datetime | None = None


# --- Hard blocks -------------------------------------------------------------


def rule_never_retry_terminal(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """Fraud and invalid accounts are never retried.

    Retrying a suspected-fraud transaction is a compliance incident, not a
    missed optimisation. This rule runs first for that reason.
    """
    if failure_class in NEVER_RETRY and planned.action is not Action.STOP:
        return Ruling(
            verdict=Verdict.VETO,
            rule_id="never_retry_terminal",
            reason=(
                f"{failure_class.value} is in NEVER_RETRY; "
                f"{planned.action.value} blocked outright."
            ),
            replacement_action=Action.STOP,
        )
    return None


def rule_chargeback_open(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """An open chargeback outranks any diagnosis. Touching the payment while a
    dispute is live is indefensible."""
    if txn.chargeback_open and planned.action is not Action.STOP:
        return Ruling(
            verdict=Verdict.VETO,
            rule_id="chargeback_open",
            reason="Chargeback is open on this transaction; all action blocked.",
            replacement_action=Action.STOP,
        )
    return None


def rule_opt_out(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """Opt-out is absolute, checked before every outbound action.

    Note it blocks only *outbound* actions. A silent retry on an opted-out
    customer is still permitted, because opting out of marketing contact is not
    opting out of the subscription they bought.
    """
    if txn.opted_out and planned.action in OUTBOUND_ACTIONS:
        return Ruling(
            verdict=Verdict.VETO,
            rule_id="opt_out",
            reason=(
                f"Customer {txn.customer_id} has opted out; "
                f"{planned.action.value} suppressed."
            ),
            replacement_action=Action.STOP,
        )
    return None


def rule_no_channel(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """No contactable channel means an outbound action cannot be delivered."""
    if planned.action in OUTBOUND_ACTIONS and not txn.channel_prefs:
        return Ruling(
            verdict=Verdict.VETO,
            rule_id="no_channel",
            reason="No channel preferences on file; outbound cannot be delivered.",
            replacement_action=Action.STOP,
        )
    return None


def rule_undiagnosed(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """An unclassified failure is handed to a human, not acted on blind.

    The classifier deliberately returns `None` rather than guessing. Honouring
    that here is the other half of the bargain: if we then retried anyway, the
    refusal to guess would be decorative.

    A DOWNGRADE rather than a VETO, because a person can read the raw gateway
    message and decide — which is also how the step 7 LLM layer earns its keep,
    by shrinking this population.
    """
    if failure_class is None and planned.action not in (
        Action.ESCALATE_HUMAN,
        Action.STOP,
    ):
        return Ruling(
            verdict=Verdict.DOWNGRADE,
            rule_id="undiagnosed",
            reason=(
                f"No failure class could be determined from code "
                f"{txn.failure_code!r}; {planned.action.value} requires a human "
                "to read the raw gateway response."
            ),
            replacement_action=Action.ESCALATE_HUMAN,
        )
    return None


# --- Budgets -----------------------------------------------------------------


def rule_retry_cap(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """At most MAX_RETRIES_PER_TXN charge attempts, counting prior history."""
    if planned.action not in RETRY_ACTIONS:
        return None
    used = state.retries_used(txn)
    if used >= MAX_RETRIES_PER_TXN:
        return Ruling(
            verdict=Verdict.VETO,
            rule_id="retry_cap",
            reason=(
                f"{used} retries already used (cap {MAX_RETRIES_PER_TXN}); "
                "giving up on this transaction."
            ),
            replacement_action=Action.STOP,
        )
    return None


def rule_message_cap_per_txn(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    if planned.action not in OUTBOUND_ACTIONS:
        return None
    sent = state.messages_for_txn(txn.txn_id)
    if sent >= MAX_CUSTOMER_MESSAGES_PER_TXN:
        return Ruling(
            verdict=Verdict.VETO,
            rule_id="message_cap_txn",
            reason=(
                f"{sent} messages already sent for this transaction "
                f"(cap {MAX_CUSTOMER_MESSAGES_PER_TXN})."
            ),
            replacement_action=Action.STOP,
        )
    return None


def rule_message_cap_per_customer(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """Cross-transaction anti-spam cap.

    A customer with four failed subscriptions in a week must not receive four
    independent dunning sequences. This is the rule that makes the "customer
    contacts sent, per-customer max" metric defensible.
    """
    if planned.action not in OUTBOUND_ACTIONS:
        return None
    sent = state.messages_for_customer_7d(txn.customer_id, now)
    if sent >= MAX_MESSAGES_PER_CUSTOMER_7D:
        return Ruling(
            verdict=Verdict.VETO,
            rule_id="message_cap_customer_7d",
            reason=(
                f"Customer {txn.customer_id} has had {sent} messages in 7 days "
                f"(cap {MAX_MESSAGES_PER_CUSTOMER_7D}); suppressing to avoid spam."
            ),
            replacement_action=Action.STOP,
        )
    return None


def rule_recovery_window(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """Stop chasing after the recovery window closes.

    A stopping rule the agent applies to itself: past 72h from the original
    failure, the transaction is abandoned and the reason is logged.
    """
    if planned.action is Action.STOP:
        return None
    age = planned.scheduled_at - txn.timestamp
    if age > RECOVERY_WINDOW:
        return Ruling(
            verdict=Verdict.VETO,
            rule_id="recovery_window",
            reason=(
                f"Action scheduled {age.total_seconds() / 3600:.0f}h after failure, "
                f"beyond the {RECOVERY_WINDOW.total_seconds() / 3600:.0f}h window."
            ),
            replacement_action=Action.STOP,
        )
    return None


# --- Escalation and timing ---------------------------------------------------


def rule_human_approval_threshold(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """High-value transactions are handed to a person, not auto-actioned.

    A DOWNGRADE rather than a VETO: the money is still pursued, but by someone
    accountable. `ESCALATE_HUMAN` and `STOP` pass through unchanged, otherwise
    the rule would fire on its own output.
    """
    if planned.action in (Action.ESCALATE_HUMAN, Action.STOP):
        return None
    if txn.amount >= HUMAN_APPROVAL_THRESHOLD:
        return Ruling(
            verdict=Verdict.DOWNGRADE,
            rule_id="human_approval_threshold",
            reason=(
                f"Rs.{txn.amount:,.0f} is at or above the "
                f"Rs.{HUMAN_APPROVAL_THRESHOLD:,.0f} approval threshold; "
                f"{planned.action.value} requires a human owner."
            ),
            replacement_action=Action.ESCALATE_HUMAN,
        )
    return None


def rule_quiet_hours(
    txn: Transaction,
    planned: PlannedAction,
    state: RunState,
    now: datetime,
    failure_class: FailureClass | None = None,
) -> Ruling | None:
    """No outbound contact 21:00-09:00 IST.

    Deferred, not cancelled: the message is still worth sending, just not at
    2am. Retries are unaffected — a silent re-attempt disturbs nobody.
    """
    if planned.action not in OUTBOUND_ACTIONS:
        return None
    if not is_quiet_hours(planned.scheduled_at):
        return None
    shifted = next_allowed_contact_time(planned.scheduled_at)
    return Ruling(
        verdict=Verdict.DEFER,
        rule_id="quiet_hours",
        reason=(
            f"{planned.scheduled_at.isoformat()} falls in quiet hours "
            f"(21:00-09:00 IST); deferred to {shifted.isoformat()}."
        ),
        replacement_time=shifted,
    )


#: Evaluation order. Hard blocks first so a vetoed action is never also
#: reported as merely deferred; timing adjustments last, once the action is
#: settled.
ORDERED_RULES = (
    rule_never_retry_terminal,
    rule_chargeback_open,
    rule_opt_out,
    rule_no_channel,
    rule_undiagnosed,
    rule_retry_cap,
    rule_message_cap_per_txn,
    rule_message_cap_per_customer,
    rule_recovery_window,
    rule_human_approval_threshold,
    rule_quiet_hours,
)

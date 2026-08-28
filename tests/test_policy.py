"""The highest-value tests in the repo.

Every guardrail in CLAUDE.md gets a test proving it actually fires, plus tests
for the properties that make the engine trustworthy: it is the only authoriser,
it never emits an action outside the enum, and it records why.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.schemas import PlannedAction, Transaction
from core.taxonomy import (
    NEVER_RETRY,
    OUTBOUND_ACTIONS,
    RETRY_ACTIONS,
    Action,
    Channel,
    FailureClass,
    MandateStatus,
    PaymentMethod,
    Verdict,
)
from policy.engine import PolicyEngine
from policy.limits import (
    HUMAN_APPROVAL_THRESHOLD,
    MAX_CUSTOMER_MESSAGES_PER_TXN,
    MAX_MESSAGES_PER_CUSTOMER_7D,
    MAX_RETRIES_PER_TXN,
    is_quiet_hours,
    next_allowed_contact_time,
)
from policy.state import RunState

NOON_UTC = datetime(2026, 8, 20, 6, 30, tzinfo=UTC)  # 12:00 IST, safely awake
MIDNIGHT_IST = datetime(2026, 8, 20, 18, 30, tzinfo=UTC)  # 00:00 IST, quiet


def _txn(**overrides) -> Transaction:
    base = dict(
        txn_id="txn_test",
        customer_id="cust_test",
        merchant_id="mrc_test",
        amount=Decimal("499.00"),
        timestamp=NOON_UTC,
        method=PaymentMethod.CARD,
        failure_code="GW_51",
        failure_message="insufficient funds in account",
        retry_count=0,
        customer_ltv=Decimal("1200.00"),
        prior_success_rate=0.9,
        prior_failures_7d=0,
        is_subscription=True,
        mandate_status=MandateStatus.NONE,
        opted_out=False,
        channel_prefs=(Channel.WHATSAPP, Channel.EMAIL),
    )
    return Transaction(**{**base, **overrides})


def _plan(action: Action, at: datetime | None = None) -> PlannedAction:
    return PlannedAction(
        txn_id="txn_test",
        action=action,
        scheduled_at=at or (NOON_UTC + timedelta(hours=2)),
        rationale="test",
    )


@pytest.fixture
def engine():
    return PolicyEngine()


@pytest.fixture
def state():
    return RunState()


# --- NEVER_RETRY -------------------------------------------------------------


@pytest.mark.parametrize("cls", sorted(NEVER_RETRY))
def test_never_retry_classes_are_blocked(engine, state, cls):
    d = engine.evaluate(_txn(), _plan(Action.RETRY), state, NOON_UTC, cls)
    assert d.verdict is Verdict.VETO
    assert d.final_action is Action.STOP
    assert d.rule_id == "never_retry_terminal"


def test_fraud_blocks_outbound_too_not_just_retries(engine, state):
    """Messaging a suspected-fraud customer is also wrong, not just retrying."""
    d = engine.evaluate(
        _txn(), _plan(Action.NUDGE_WHATSAPP), state, NOON_UTC,
        FailureClass.SUSPECTED_FRAUD,
    )
    assert d.verdict is Verdict.VETO


def test_fraud_veto_beats_a_perfect_customer(engine, state):
    """No combination of good signals unblocks a NEVER_RETRY class."""
    good = _txn(prior_success_rate=1.0, customer_ltv=Decimal("99999"), retry_count=0)
    d = engine.evaluate(good, _plan(Action.RETRY), state, NOON_UTC,
                        FailureClass.SUSPECTED_FRAUD)
    assert d.verdict is Verdict.VETO


# --- Open chargeback ---------------------------------------------------------


def test_open_chargeback_blocks_everything(engine, state):
    d = engine.evaluate(
        _txn(chargeback_open=True), _plan(Action.RETRY), state, NOON_UTC,
        FailureClass.ISSUER_DOWN,
    )
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "chargeback_open"


# --- Opt-out -----------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(OUTBOUND_ACTIONS))
def test_opt_out_suppresses_every_outbound_action(engine, state, action):
    d = engine.evaluate(
        _txn(opted_out=True), _plan(action), state, NOON_UTC,
        FailureClass.CHECKOUT_ABANDONED,
    )
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "opt_out"
    assert d.final_action is Action.STOP


def test_opt_out_does_not_block_a_silent_retry(engine, state):
    """Opting out of contact is not opting out of the subscription."""
    d = engine.evaluate(
        _txn(opted_out=True), _plan(Action.RETRY), state, NOON_UTC,
        FailureClass.ISSUER_DOWN,
    )
    assert d.verdict is Verdict.ALLOW
    assert d.final_action is Action.RETRY


def test_no_channel_blocks_outbound(engine, state):
    d = engine.evaluate(
        _txn(channel_prefs=()), _plan(Action.NUDGE_EMAIL), state, NOON_UTC,
        FailureClass.CHECKOUT_ABANDONED,
    )
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "no_channel"


# --- Retry cap ---------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(RETRY_ACTIONS))
def test_retry_cap_fires_on_prior_history(engine, state, action):
    d = engine.evaluate(
        _txn(retry_count=MAX_RETRIES_PER_TXN), _plan(action), state, NOON_UTC,
        FailureClass.ISSUER_DOWN,
    )
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "retry_cap"
    assert d.final_action is Action.STOP


def test_retry_cap_counts_retries_executed_during_the_run(engine, state):
    """The cap must be reachable mid-batch, not only across batches."""
    txn = _txn(retry_count=0)
    for _ in range(MAX_RETRIES_PER_TXN):
        d = engine.evaluate(txn, _plan(Action.RETRY), state, NOON_UTC,
                            FailureClass.ISSUER_DOWN)
        assert d.verdict is Verdict.ALLOW
        state.record(txn, Action.RETRY, NOON_UTC)

    d = engine.evaluate(txn, _plan(Action.RETRY), state, NOON_UTC,
                        FailureClass.ISSUER_DOWN)
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "retry_cap"


def test_retry_cap_does_not_block_messages(engine, state):
    d = engine.evaluate(
        _txn(retry_count=5), _plan(Action.NUDGE_EMAIL), state, NOON_UTC,
        FailureClass.CHECKOUT_ABANDONED,
    )
    assert d.verdict is not Verdict.VETO


# --- Message caps ------------------------------------------------------------


def test_message_cap_per_transaction(engine, state):
    txn = _txn()
    for _ in range(MAX_CUSTOMER_MESSAGES_PER_TXN):
        d = engine.evaluate(txn, _plan(Action.NUDGE_EMAIL), state, NOON_UTC,
                            FailureClass.CHECKOUT_ABANDONED)
        assert d.verdict is not Verdict.VETO
        state.record(txn, Action.NUDGE_EMAIL, NOON_UTC)

    d = engine.evaluate(txn, _plan(Action.NUDGE_EMAIL), state, NOON_UTC,
                        FailureClass.CHECKOUT_ABANDONED)
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "message_cap_txn"


def test_message_cap_per_customer_spans_transactions(engine, state):
    """The anti-spam guardrail: four failed subscriptions must not mean four
    dunning sequences."""
    txns = [_txn(txn_id=f"txn_{i}", customer_id="cust_shared") for i in range(4)]
    for txn in txns[:MAX_MESSAGES_PER_CUSTOMER_7D]:
        d = engine.evaluate(txn, _plan(Action.NUDGE_WHATSAPP), state, NOON_UTC,
                            FailureClass.CHECKOUT_ABANDONED)
        assert d.verdict is not Verdict.VETO
        state.record(txn, Action.NUDGE_WHATSAPP, NOON_UTC)

    d = engine.evaluate(txns[-1], _plan(Action.NUDGE_WHATSAPP), state, NOON_UTC,
                        FailureClass.CHECKOUT_ABANDONED)
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "message_cap_customer_7d"


def test_customer_message_cap_ages_out_after_seven_days(engine, state):
    txn = _txn()
    old = NOON_UTC - timedelta(days=8)
    state.seed_customer_history(txn.customer_id, [old] * 5)
    d = engine.evaluate(txn, _plan(Action.NUDGE_EMAIL), state, NOON_UTC,
                        FailureClass.CHECKOUT_ABANDONED)
    assert d.verdict is not Verdict.VETO


# --- Recovery window ---------------------------------------------------------


def test_recovery_window_stops_late_actions(engine, state):
    late = _plan(Action.RETRY, at=NOON_UTC + timedelta(hours=73))
    d = engine.evaluate(_txn(), late, state, NOON_UTC, FailureClass.ISSUER_DOWN)
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "recovery_window"


def test_action_inside_the_window_is_allowed(engine, state):
    ontime = _plan(Action.RETRY, at=NOON_UTC + timedelta(hours=71))
    d = engine.evaluate(_txn(), ontime, state, NOON_UTC, FailureClass.ISSUER_DOWN)
    assert d.verdict is Verdict.ALLOW


# --- Human approval threshold ------------------------------------------------


def test_high_value_is_escalated_not_auto_actioned(engine, state):
    d = engine.evaluate(
        _txn(amount=Decimal("120000.00")), _plan(Action.RETRY), state, NOON_UTC,
        FailureClass.ISSUER_DOWN,
    )
    assert d.verdict is Verdict.DOWNGRADE
    assert d.final_action is Action.ESCALATE_HUMAN
    assert d.rule_id == "human_approval_threshold"


def test_threshold_is_inclusive(engine, state):
    d = engine.evaluate(
        _txn(amount=HUMAN_APPROVAL_THRESHOLD), _plan(Action.RETRY), state,
        NOON_UTC, FailureClass.ISSUER_DOWN,
    )
    assert d.final_action is Action.ESCALATE_HUMAN


def test_just_below_threshold_is_not_escalated(engine, state):
    d = engine.evaluate(
        _txn(amount=HUMAN_APPROVAL_THRESHOLD - Decimal("1")), _plan(Action.RETRY),
        state, NOON_UTC, FailureClass.ISSUER_DOWN,
    )
    assert d.final_action is Action.RETRY


def test_escalation_does_not_re_trigger_itself(engine, state):
    """The rule must not fire on its own output, or it would loop."""
    d = engine.evaluate(
        _txn(amount=Decimal("120000.00")), _plan(Action.ESCALATE_HUMAN), state,
        NOON_UTC, FailureClass.ISSUER_DOWN,
    )
    assert d.final_action is Action.ESCALATE_HUMAN
    assert d.verdict is Verdict.ALLOW


def test_fraud_veto_outranks_high_value_escalation(engine, state):
    """A Rs.1.2L fraud transaction is blocked, not escalated."""
    d = engine.evaluate(
        _txn(amount=Decimal("120000.00")), _plan(Action.RETRY), state, NOON_UTC,
        FailureClass.SUSPECTED_FRAUD,
    )
    assert d.verdict is Verdict.VETO
    assert d.final_action is Action.STOP


# --- Quiet hours -------------------------------------------------------------


def test_quiet_hours_defers_outbound(engine, state):
    plan = _plan(Action.NUDGE_WHATSAPP, at=MIDNIGHT_IST)
    d = engine.evaluate(_txn(timestamp=MIDNIGHT_IST - timedelta(hours=1)),
                        plan, state, MIDNIGHT_IST, FailureClass.CHECKOUT_ABANDONED)
    assert d.verdict is Verdict.DEFER
    assert d.rule_id == "quiet_hours"
    assert d.final_scheduled_at > plan.scheduled_at
    assert not is_quiet_hours(d.final_scheduled_at)


def test_quiet_hours_does_not_defer_retries(engine, state):
    """A silent re-attempt at 2am disturbs nobody."""
    plan = _plan(Action.RETRY, at=MIDNIGHT_IST)
    d = engine.evaluate(_txn(timestamp=MIDNIGHT_IST - timedelta(hours=1)),
                        plan, state, MIDNIGHT_IST, FailureClass.ISSUER_DOWN)
    assert d.verdict is Verdict.ALLOW
    assert d.final_scheduled_at == plan.scheduled_at


@pytest.mark.parametrize("ist_hour,quiet", [
    (0, True), (5, True), (8, True), (9, False), (12, False),
    (20, False), (21, True), (23, True),
])
def test_quiet_hours_boundaries(ist_hour, quiet):
    from policy.limits import IST
    dt = datetime(2026, 8, 20, ist_hour, 30, tzinfo=IST)
    assert is_quiet_hours(dt) is quiet


def test_deferral_lands_at_nine_am_ist():
    from policy.limits import IST
    at_2am = datetime(2026, 8, 20, 2, 0, tzinfo=IST)
    shifted = next_allowed_contact_time(at_2am)
    assert shifted.astimezone(IST).hour == 9
    assert shifted.astimezone(IST).day == 20


def test_late_evening_defers_to_next_morning():
    from policy.limits import IST
    at_10pm = datetime(2026, 8, 20, 22, 0, tzinfo=IST)
    shifted = next_allowed_contact_time(at_10pm)
    assert shifted.astimezone(IST).hour == 9
    assert shifted.astimezone(IST).day == 21


# --- Engine properties -------------------------------------------------------


def test_veto_short_circuits_and_reports_one_reason(engine, state):
    """A blocked action must not also be reported as deferred."""
    txn = _txn(opted_out=True, chargeback_open=True)
    d = engine.evaluate(txn, _plan(Action.NUDGE_WHATSAPP, at=MIDNIGHT_IST),
                        state, MIDNIGHT_IST, FailureClass.SUSPECTED_FRAUD)
    assert d.verdict is Verdict.VETO
    assert d.rule_id == "never_retry_terminal"


def test_every_decision_is_auditable(engine, state):
    d = engine.evaluate(_txn(), _plan(Action.RETRY), state, NOON_UTC,
                        FailureClass.ISSUER_DOWN)
    assert d.decision_id
    assert d.txn_id == "txn_test"
    assert d.decided_at == NOON_UTC
    assert d.rule_id
    assert d.reason
    assert d.proposed_action is Action.RETRY


def test_engine_never_emits_an_action_outside_the_enum(engine, state):
    for cls in FailureClass:
        for action in Action:
            d = engine.evaluate(_txn(), _plan(action), state, NOON_UTC, cls)
            assert d.final_action in set(Action)


def test_authorise_or_stop_refuses_stop_and_vetoes(engine, state):
    _, may = engine.authorise_or_stop(
        _txn(), _plan(Action.STOP), state, NOON_UTC, FailureClass.ISSUER_DOWN
    )
    assert may is False

    _, may = engine.authorise_or_stop(
        _txn(opted_out=True), _plan(Action.NUDGE_EMAIL), state, NOON_UTC,
        FailureClass.CHECKOUT_ABANDONED,
    )
    assert may is False

    _, may = engine.authorise_or_stop(
        _txn(), _plan(Action.RETRY), state, NOON_UTC, FailureClass.ISSUER_DOWN
    )
    assert may is True


def test_undiagnosed_transactions_are_not_auto_actioned(engine, state):
    """failure_class=None means the rules could not classify it. Retrying an
    unknown failure is exactly the guessing the classifier refused to do."""
    d = engine.evaluate(_txn(), _plan(Action.RETRY), state, NOON_UTC, None)
    assert d.verdict is Verdict.DOWNGRADE
    assert d.final_action is Action.ESCALATE_HUMAN
    assert d.rule_id == "undiagnosed"


# --- Coverage of the guardrail set ------------------------------------------


def test_outbound_action_set_is_exhaustive():
    """Any action that contacts a human must be in OUTBOUND_ACTIONS, or it
    silently bypasses opt-out, quiet hours, and both message caps."""
    contacting = {
        Action.SEND_PAYMENT_LINK,
        Action.NUDGE_WHATSAPP,
        Action.NUDGE_EMAIL,
        Action.REQUEST_INSTRUMENT_UPDATE,
        Action.REQUEST_MANDATE_RENEWAL,
        Action.OFFER_PARTIAL_PLAN,
    }
    assert OUTBOUND_ACTIONS == contacting


def test_every_guardrail_constant_has_an_enforcing_rule():
    """Guards against a limit being defined but never wired up."""
    from policy.rules import ORDERED_RULES

    rule_ids = set()
    engine = PolicyEngine()
    state = RunState()

    scenarios = [
        (_txn(), Action.RETRY, FailureClass.SUSPECTED_FRAUD, NOON_UTC),
        (_txn(chargeback_open=True), Action.RETRY, FailureClass.ISSUER_DOWN, NOON_UTC),
        (_txn(opted_out=True), Action.NUDGE_EMAIL, FailureClass.CHECKOUT_ABANDONED, NOON_UTC),
        (_txn(channel_prefs=()), Action.NUDGE_EMAIL, FailureClass.CHECKOUT_ABANDONED, NOON_UTC),
        (_txn(retry_count=2), Action.RETRY, FailureClass.ISSUER_DOWN, NOON_UTC),
        (_txn(amount=Decimal("120000")), Action.RETRY, FailureClass.ISSUER_DOWN, NOON_UTC),
    ]
    for txn, action, cls, now in scenarios:
        rule_ids.add(engine.evaluate(txn, _plan(action), state, now, cls).rule_id)

    for expected in (
        "never_retry_terminal", "chargeback_open", "opt_out", "no_channel",
        "retry_cap", "human_approval_threshold",
    ):
        assert expected in rule_ids, f"{expected} never fired"

    assert len(ORDERED_RULES) == 12

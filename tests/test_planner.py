"""Planner: distinct failure classes must produce distinct behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.schemas import Diagnosis, RecoverabilityAssessment, Transaction
from core.taxonomy import (
    Action,
    Channel,
    FailureClass,
    MandateStatus,
    PaymentMethod,
    Recoverability,
)
from plan.planner import MIN_SCORE_TO_ACT, plan
from policy.limits import RECOVERY_WINDOW

NOW = datetime(2026, 8, 20, 6, 30, tzinfo=UTC)


def _txn(**overrides) -> Transaction:
    base = dict(
        txn_id="txn_test", customer_id="cust_test", merchant_id="mrc_test",
        amount=Decimal("499.00"), timestamp=NOW, method=PaymentMethod.CARD,
        failure_code="GW_51", failure_message="insufficient funds",
        retry_count=0, customer_ltv=Decimal("1200.00"), prior_success_rate=0.9,
        prior_failures_7d=0, is_subscription=True,
        mandate_status=MandateStatus.NONE, opted_out=False,
        channel_prefs=(Channel.WHATSAPP, Channel.EMAIL),
    )
    return Transaction(**{**base, **overrides})


def _diag(cls, confidence=0.95):
    return Diagnosis(txn_id="txn_test", failure_class=cls, confidence=confidence,
                     rule_id="test", rationale="test")


def _score(score=0.7):
    return RecoverabilityAssessment(
        txn_id="txn_test", score=score, tier=Recoverability.HIGH, confidence=0.95
    )


def test_distinct_classes_produce_distinct_actions():
    """If every class produced RETRY, the taxonomy would be decoration."""
    actions = {}
    for cls in [
        FailureClass.ISSUER_DOWN, FailureClass.INSUFFICIENT_FUNDS,
        FailureClass.CARD_EXPIRED, FailureClass.MANDATE_EXPIRED,
        FailureClass.THREE_DS_TIMEOUT, FailureClass.CHECKOUT_ABANDONED,
    ]:
        p = plan(_txn(), _diag(cls), _score(), NOW)
        actions[cls] = p.action
    assert len(set(actions.values())) >= 5, actions


def test_issuer_down_retries_at_two_then_six_hours():
    first = plan(_txn(), _diag(FailureClass.ISSUER_DOWN), _score(), NOW, 1)
    second = plan(_txn(), _diag(FailureClass.ISSUER_DOWN), _score(), NOW, 2)
    assert first.action is Action.RETRY
    assert first.scheduled_at == NOW + timedelta(hours=2)
    assert second.scheduled_at == NOW + timedelta(hours=6)


def test_issuer_down_ladder_ends_after_two_rungs():
    assert plan(_txn(), _diag(FailureClass.ISSUER_DOWN), _score(), NOW, 3) is None


def test_insufficient_funds_never_retries_immediately():
    """Retrying on the spot burns issuer trust and cannot succeed: the balance
    has not changed."""
    p = plan(_txn(), _diag(FailureClass.INSUFFICIENT_FUNDS), _score(), NOW, 1)
    assert p.action is Action.RETRY_SALARY_ALIGNED
    assert p.scheduled_at > NOW


def test_salary_alignment_uses_payday_when_it_falls_in_the_window():
    txn = _txn(salary_day=21, timestamp=NOW)
    p = plan(txn, _diag(FailureClass.INSUFFICIENT_FUNDS), _score(), NOW, 1)
    assert p.scheduled_at <= NOW + RECOVERY_WINDOW
    assert "payday" in p.rationale.lower()


def test_salary_alignment_falls_back_when_payday_is_unknown():
    txn = _txn(salary_day=None)
    p = plan(txn, _diag(FailureClass.INSUFFICIENT_FUNDS), _score(), NOW, 1)
    assert "fallback" in p.rationale.lower()
    assert p.scheduled_at <= NOW + RECOVERY_WINDOW


def test_salary_alignment_never_schedules_past_the_recovery_window():
    """A payday three weeks out must not produce an action the window vetoes."""
    for day in range(1, 32):
        txn = _txn(salary_day=day)
        p = plan(txn, _diag(FailureClass.INSUFFICIENT_FUNDS), _score(), NOW, 1)
        assert p.scheduled_at <= txn.timestamp + RECOVERY_WINDOW, day


def test_card_expired_asks_the_customer_rather_than_retrying():
    p = plan(_txn(), _diag(FailureClass.CARD_EXPIRED), _score(), NOW, 1)
    assert p.action is Action.REQUEST_INSTRUMENT_UPDATE


def test_mandate_classes_request_renewal():
    for cls in (FailureClass.MANDATE_EXPIRED, FailureClass.MANDATE_REVOKED):
        p = plan(_txn(), _diag(cls), _score(), NOW, 1)
        assert p.action is Action.REQUEST_MANDATE_RENEWAL


def test_3ds_timeout_moves_fast_because_intent_was_present():
    p = plan(_txn(), _diag(FailureClass.THREE_DS_TIMEOUT), _score(), NOW, 1)
    assert p.action is Action.SEND_PAYMENT_LINK
    assert p.scheduled_at == NOW + timedelta(minutes=30)


def test_do_not_honour_gets_one_retry_then_the_ladder_ends():
    assert plan(_txn(), _diag(FailureClass.DO_NOT_HONOUR), _score(), NOW, 1).action \
        is Action.RETRY
    assert plan(_txn(), _diag(FailureClass.DO_NOT_HONOUR), _score(), NOW, 2) is None


def test_terminal_classes_plan_a_stop():
    for cls in (FailureClass.SUSPECTED_FRAUD, FailureClass.INVALID_ACCOUNT):
        p = plan(_txn(), _diag(cls), _score(0.0), NOW, 1)
        assert p.action is Action.STOP


def test_undiagnosed_is_escalated_not_guessed():
    p = plan(_txn(), _diag(None, confidence=0.0), _score(), NOW, 1)
    assert p.action is Action.ESCALATE_HUMAN


def test_low_recoverability_stops_before_policy_is_consulted():
    """The agent's own stopping rule: not worth chasing."""
    p = plan(_txn(), _diag(FailureClass.DO_NOT_HONOUR),
             _score(MIN_SCORE_TO_ACT - 0.01), NOW, 1)
    assert p.action is Action.STOP
    assert "below" in p.rationale


def test_nudge_channel_follows_customer_preference():
    whatsapp = plan(_txn(channel_prefs=(Channel.WHATSAPP,)),
                    _diag(FailureClass.CHECKOUT_ABANDONED), _score(), NOW, 1)
    email = plan(_txn(channel_prefs=(Channel.EMAIL,)),
                 _diag(FailureClass.CHECKOUT_ABANDONED), _score(), NOW, 1)
    assert whatsapp.action is Action.NUDGE_WHATSAPP
    assert email.action is Action.NUDGE_EMAIL


def test_planner_never_emits_an_action_outside_the_enum():
    for cls in FailureClass:
        for attempt in (1, 2, 3):
            p = plan(_txn(), _diag(cls), _score(), NOW, attempt)
            if p is not None:
                assert p.action in set(Action)


def test_every_plan_carries_a_rationale():
    for cls in FailureClass:
        p = plan(_txn(), _diag(cls), _score(), NOW, 1)
        if p is not None:
            assert p.rationale

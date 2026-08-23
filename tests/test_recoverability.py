"""Recoverability scoring: hard zeros stay zero, signals move the score sanely."""

from __future__ import annotations

from decimal import Decimal

from core.schemas import Diagnosis, Transaction
from core.taxonomy import Channel, FailureClass, MandateStatus, PaymentMethod, Recoverability
from diagnose.recoverability import assess


def _txn(**overrides) -> Transaction:
    base = dict(
        txn_id="txn_test",
        customer_id="cust_test",
        merchant_id="mrc_test",
        amount=Decimal("499.00"),
        timestamp="2026-08-20T10:00:00Z",
        method=PaymentMethod.CARD,
        failure_code="GW_51",
        failure_message="insufficient funds in account",
        retry_count=0,
        customer_ltv=Decimal("1200.00"),
        prior_success_rate=0.80,
        prior_failures_7d=0,
        is_subscription=True,
        mandate_status=MandateStatus.NONE,
        opted_out=False,
        channel_prefs=(Channel.EMAIL,),
    )
    return Transaction(**{**base, **overrides})


def _diag(cls: FailureClass | None, confidence: float = 0.95) -> Diagnosis:
    return Diagnosis(
        txn_id="txn_test",
        failure_class=cls,
        confidence=confidence,
        rule_id="test",
        rationale="test",
    )


# --- Hard zeros --------------------------------------------------------------


def test_fraud_scores_zero():
    a = assess(_txn(), _diag(FailureClass.SUSPECTED_FRAUD))
    assert a.score == 0.0
    assert a.tier is Recoverability.NEVER


def test_invalid_account_scores_zero():
    assert assess(_txn(), _diag(FailureClass.INVALID_ACCOUNT)).score == 0.0


def test_open_chargeback_scores_zero_regardless_of_class():
    """CHARGEBACK_OPEN outranks even a high-recoverability diagnosis."""
    a = assess(_txn(chargeback_open=True), _diag(FailureClass.ISSUER_DOWN))
    assert a.score == 0.0
    assert "chargeback" in a.factors[0].lower()


def test_undiagnosed_scores_zero():
    a = assess(_txn(), _diag(None, confidence=0.0))
    assert a.score == 0.0
    assert "undiagnosed" in a.factors[0]


def test_hard_zeros_cannot_be_argued_up_by_good_signals():
    """A perfect customer with a fraud flag is still zero."""
    perfect = _txn(prior_success_rate=1.0, retry_count=0, prior_failures_7d=0)
    assert assess(perfect, _diag(FailureClass.SUSPECTED_FRAUD)).score == 0.0


# --- Graded signals ----------------------------------------------------------


def test_issuer_down_outscores_do_not_honour():
    """Distinct classes must produce visibly different scores, or the whole
    taxonomy is decoration."""
    high = assess(_txn(), _diag(FailureClass.ISSUER_DOWN)).score
    low = assess(_txn(), _diag(FailureClass.DO_NOT_HONOUR)).score
    assert high > low


def test_prior_retries_lower_the_score():
    none = assess(_txn(retry_count=0), _diag(FailureClass.ISSUER_DOWN)).score
    two = assess(_txn(retry_count=2), _diag(FailureClass.ISSUER_DOWN)).score
    assert two < none


def test_repeat_failures_lower_the_score():
    clean = assess(_txn(prior_failures_7d=0), _diag(FailureClass.ISSUER_DOWN)).score
    repeat = assess(_txn(prior_failures_7d=4), _diag(FailureClass.ISSUER_DOWN)).score
    assert repeat < clean


def test_good_payment_history_raises_the_score():
    poor = assess(_txn(prior_success_rate=0.4), _diag(FailureClass.ISSUER_DOWN)).score
    good = assess(_txn(prior_success_rate=1.0), _diag(FailureClass.ISSUER_DOWN)).score
    assert good > poor


def test_opted_out_lowers_score_for_contact_dependent_classes():
    """CARD_EXPIRED can only be fixed by the customer, so an unreachable
    customer is materially less recoverable."""
    reachable = assess(_txn(), _diag(FailureClass.CARD_EXPIRED)).score
    opted_out = assess(_txn(opted_out=True), _diag(FailureClass.CARD_EXPIRED)).score
    assert opted_out < reachable


def test_opt_out_does_not_penalise_classes_that_need_no_contact():
    """ISSUER_DOWN is fixed by a retry, so opting out is irrelevant to it."""
    reachable = assess(_txn(), _diag(FailureClass.ISSUER_DOWN)).score
    opted_out = assess(_txn(opted_out=True), _diag(FailureClass.ISSUER_DOWN)).score
    assert opted_out == reachable


def test_no_channels_is_treated_as_unreachable():
    a = assess(_txn(channel_prefs=()), _diag(FailureClass.CARD_EXPIRED))
    assert any("unreachable" in f for f in a.factors)


# --- Invariants and auditability ---------------------------------------------


def test_score_stays_in_range_under_extreme_inputs():
    worst = _txn(prior_success_rate=0.0, retry_count=9, prior_failures_7d=9,
                 opted_out=True, channel_prefs=())
    best = _txn(prior_success_rate=1.0, retry_count=0, prior_failures_7d=0)
    for cls in FailureClass:
        assert 0.0 <= assess(worst, _diag(cls)).score <= 1.0
        assert 0.0 <= assess(best, _diag(cls)).score <= 1.0


def test_confidence_is_carried_not_blended():
    """A confident low score and a guessed low score must stay distinguishable."""
    confident = assess(_txn(), _diag(FailureClass.DO_NOT_HONOUR, confidence=0.95))
    guessed = assess(_txn(), _diag(FailureClass.DO_NOT_HONOUR, confidence=0.55))
    assert confident.score == guessed.score
    assert confident.confidence != guessed.confidence


def test_every_adjustment_is_recorded_for_the_audit_trail():
    a = assess(
        _txn(retry_count=2, prior_failures_7d=3, prior_success_rate=0.5),
        _diag(FailureClass.ISSUER_DOWN),
    )
    joined = " ".join(a.factors)
    assert "base" in joined
    assert "retries" in joined
    assert "failures in 7d" in joined
    assert "prior success rate" in joined

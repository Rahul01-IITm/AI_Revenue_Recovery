"""Rules-based classification, including the ambiguity it is built to handle."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.schemas import Transaction
from core.taxonomy import Channel, FailureClass, MandateStatus, PaymentMethod
from data.generator import generate_batch
from diagnose.classifier import (
    CONF_AMBIGUOUS_DEFAULT,
    CONF_AMBIGUOUS_RESOLVED,
    CONF_MESSAGE_ONLY,
    CONF_UNAMBIGUOUS_CODE,
    classify,
)
from report.diagnosis import evaluate


def _txn(code: str, message: str, **overrides) -> Transaction:
    base = dict(
        txn_id="txn_test",
        customer_id="cust_test",
        merchant_id="mrc_test",
        amount=Decimal("499.00"),
        timestamp="2026-08-20T10:00:00Z",
        method=PaymentMethod.CARD,
        failure_code=code,
        failure_message=message,
        retry_count=0,
        customer_ltv=Decimal("1200.00"),
        prior_success_rate=0.9,
        prior_failures_7d=0,
        is_subscription=True,
        mandate_status=MandateStatus.NONE,
        opted_out=False,
        channel_prefs=(Channel.EMAIL,),
    )
    return Transaction(**{**base, **overrides})


# --- Unambiguous codes -------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("GW_51", FailureClass.INSUFFICIENT_FUNDS),
        ("GW_91", FailureClass.ISSUER_DOWN),
        ("GW_TO", FailureClass.ISSUER_DOWN),
        ("GW_54", FailureClass.CARD_EXPIRED),
        ("GW_3DS", FailureClass.THREE_DS_TIMEOUT),
        ("GW_ABD", FailureClass.CHECKOUT_ABANDONED),
        ("GW_FRD", FailureClass.SUSPECTED_FRAUD),
        ("GW_14", FailureClass.INVALID_ACCOUNT),
    ],
)
def test_unambiguous_codes(code, expected):
    d = classify(_txn(code, "some gateway text"))
    assert d.failure_class is expected
    assert d.confidence == CONF_UNAMBIGUOUS_CODE


def test_code_matching_is_case_and_space_insensitive():
    assert classify(_txn(" gw_51 ", "x")).failure_class is (
        FailureClass.INSUFFICIENT_FUNDS
    )


# --- Ambiguity: the reason this module exists --------------------------------


def test_gw05_resolves_to_insufficient_funds_on_balance_wording():
    d = classify(_txn("GW_05", "do not honour - insufficient balance per issuer"))
    assert d.failure_class is FailureClass.INSUFFICIENT_FUNDS
    assert d.confidence == CONF_AMBIGUOUS_RESOLVED
    assert "insufficient" in d.rule_id


def test_gw05_falls_back_to_do_not_honour():
    d = classify(_txn("GW_05", "Declined by issuer without reason code"))
    assert d.failure_class is FailureClass.DO_NOT_HONOUR
    assert d.confidence == CONF_AMBIGUOUS_DEFAULT


def test_mandate_revoked_beats_expired_on_wording():
    d = classify(_txn("GW_MND", "mandate revoked by customer"))
    assert d.failure_class is FailureClass.MANDATE_REVOKED
    assert d.confidence == CONF_AMBIGUOUS_RESOLVED


def test_mandate_expired_wording():
    d = classify(_txn("GW_MND", "e-mandate validity period has elapsed"))
    assert d.failure_class is FailureClass.MANDATE_EXPIRED
    assert d.confidence == CONF_AMBIGUOUS_RESOLVED


def test_ambiguous_default_is_less_confident_than_resolved():
    """Calibration: a guess must announce itself as one."""
    assert CONF_AMBIGUOUS_DEFAULT < CONF_AMBIGUOUS_RESOLVED < CONF_UNAMBIGUOUS_CODE


# --- Unknown codes and refusal to guess --------------------------------------


def test_unknown_code_falls_back_to_message_keywords():
    d = classify(_txn("GW_WAT", "card reported stolen"))
    assert d.failure_class is FailureClass.SUSPECTED_FRAUD
    assert d.confidence == CONF_MESSAGE_ONLY


def test_unclassifiable_returns_none_rather_than_guessing():
    d = classify(_txn("GW_???", "something entirely unexpected happened"))
    assert d.failure_class is None
    assert d.confidence == 0.0
    assert not d.is_classified
    assert "undiagnosed" in d.rationale or "No rule matched" in d.rationale


def test_every_diagnosis_carries_audit_fields():
    d = classify(_txn("GW_51", "insufficient funds in account"))
    assert d.rule_id
    assert d.rationale
    assert d.source == "rules"


def test_fraud_keyword_beats_generic_decline_wording():
    """Ordering matters: a stolen card must never be read as a soft decline."""
    d = classify(_txn("GW_XXX", "declined - card reported stolen"))
    assert d.failure_class is FailureClass.SUSPECTED_FRAUD


# --- Against the generated batch ---------------------------------------------


def test_classifier_never_reads_ground_truth():
    """Same observable inputs must give the same answer regardless of the true
    class, which is the property that makes the accuracy number meaningful."""
    a = classify(_txn("GW_05", "do not honour"))
    b = classify(_txn("GW_05", "do not honour", txn_id="txn_other"))
    assert a.failure_class == b.failure_class
    assert a.confidence == b.confidence


def test_accuracy_on_held_out_split_is_strong_but_not_perfect():
    """Strong, and deliberately short of perfect.

    The generator emits undecidable and unrecognisable signals on purpose. A
    rules layer scoring 100% here would mean the batch was built to suit the
    rules, which is the same circularity trap as a self-tuned simulator.
    """
    batch = generate_batch(n=500, seed=42)
    report = evaluate(batch, split="test")
    assert 0.85 < report.accuracy < 1.0, f"accuracy {report.accuracy:.1%}"
    assert report.coverage < 1.0, "no unclassified rows means no step-7 target"


def test_all_errors_are_explainable():
    """Every mistake must come from genuine gateway ambiguity or from a signal
    no rule claims to handle. Errors outside those two buckets mean the rules
    are wrong rather than the data being hard."""
    batch = generate_batch(n=500, seed=42)
    report = evaluate(batch, split=None)
    ambiguous_pairs = {
        ("DO_NOT_HONOUR", "INSUFFICIENT_FUNDS"),
        ("INSUFFICIENT_FUNDS", "DO_NOT_HONOUR"),
        ("MANDATE_REVOKED", "MANDATE_EXPIRED"),
        ("MANDATE_EXPIRED", "MANDATE_REVOKED"),
    }
    unexplained = [
        (a, b, n)
        for a, b, n in report.confusions
        if b != "UNCLASSIFIED" and (a, b) not in ambiguous_pairs
    ]
    assert not unexplained, f"unexpected misclassifications: {unexplained}"


def test_unclassified_population_exists_for_step_7():
    """The LLM layer needs something to improve, and the planner needs the
    undiagnosed path exercised."""
    batch = generate_batch(n=500, seed=42)
    report = evaluate(batch, split=None)
    assert 0 < report.total - report.classified < report.total * 0.10


def test_confidence_is_monotone_with_accuracy():
    """The headline calibration claim, asserted rather than eyeballed."""
    batch = generate_batch(n=500, seed=42)
    report = evaluate(batch, split=None)
    rates = [
        ok / n
        for _, (ok, n) in sorted(report.accuracy_by_confidence.items())
        if n > 0
    ]
    assert rates == sorted(rates), f"accuracy not monotone in confidence: {rates}"


def test_accuracy_falls_with_confidence():
    """Calibration check: the confident band must beat the guessing band."""
    batch = generate_batch(n=500, seed=42)
    report = evaluate(batch, split=None)
    bands = report.accuracy_by_confidence

    high = next((v for k, v in bands.items() if k.startswith("0.90")), None)
    defaulted = next((v for k, v in bands.items() if k.startswith("0.50")), None)
    if high and defaulted and defaulted[1] > 0:
        assert (high[0] / high[1]) > (defaulted[0] / defaulted[1])

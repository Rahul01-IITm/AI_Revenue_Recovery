"""Generator guarantees: reproducibility, coherence, and the planted cases.

If reproducibility breaks, every number ever quoted from this project becomes
unverifiable. These tests are load-bearing.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest

from data.generator import FAILURE_MIX, generate_batch
from core.taxonomy import FailureClass, MandateStatus, PaymentMethod


@pytest.fixture(scope="module")
def batch():
    return generate_batch(n=500, seed=42)


# --- Reproducibility ---------------------------------------------------------


def test_same_seed_is_byte_identical():
    a = generate_batch(n=200, seed=42)
    b = generate_batch(n=200, seed=42)
    assert a.model_dump_json() == b.model_dump_json()


def test_different_seed_changes_the_batch():
    a = generate_batch(n=200, seed=42)
    b = generate_batch(n=200, seed=43)
    assert a.model_dump_json() != b.model_dump_json()


def test_no_wall_clock_leaks_into_the_batch():
    """`generated_at` must be the fixed reference time, not `now()`."""
    a = generate_batch(n=50, seed=42)
    b = generate_batch(n=50, seed=42)
    assert a.generated_at == b.generated_at


def test_split_is_stable_when_batch_grows():
    """Growing 500 -> 1000 must not reshuffle which of the first 500 are test.

    An index-based split would silently move rows between train and test and
    invalidate previously reported numbers.
    """
    small = generate_batch(n=500, seed=42)
    large = generate_batch(n=1000, seed=42)
    for txn_id in list(small.ground_truth)[:500]:
        if txn_id in large.ground_truth:
            assert small.split_of(txn_id) == large.split_of(txn_id)


# --- Shape -------------------------------------------------------------------


def test_batch_size_and_unique_ids(batch):
    assert len(batch.transactions) == 500
    assert len({t.txn_id for t in batch.transactions}) == 500


def test_both_splits_are_populated(batch):
    assert len(batch.select("test")) > 0
    assert len(batch.select("train")) > 0
    assert len(batch.select("test")) + len(batch.select("train")) == 500


def test_failure_mix_is_roughly_as_specified(batch):
    """Sampling noise is fine; a wrong distribution is not."""
    counts = Counter(
        batch.true_class(t.txn_id) for t in batch.transactions
    )
    for cls, expected in FAILURE_MIX.items():
        actual = counts[cls] / 500
        assert abs(actual - expected) < 0.05, f"{cls}: {actual:.3f} vs {expected}"


def test_fraud_is_rare(batch):
    """A uniform distribution reads as synthetic. Fraud must stay ~2%."""
    counts = Counter(batch.true_class(t.txn_id) for t in batch.transactions)
    assert counts[FailureClass.SUSPECTED_FRAUD] / 500 < 0.05


def test_b2b_is_out_of_scope(batch):
    """D2C vertical: the taxonomy row exists but no rows are generated."""
    classes = {batch.true_class(t.txn_id) for t in batch.transactions}
    assert FailureClass.B2B_INVOICE_OVERDUE not in classes


# --- Row-level coherence -----------------------------------------------------


def test_mandate_rows_are_internally_consistent(batch):
    for t in batch.transactions:
        cls = batch.true_class(t.txn_id)
        if cls is FailureClass.MANDATE_EXPIRED:
            assert t.mandate_status is MandateStatus.EXPIRED
            assert t.method is PaymentMethod.EMANDATE
            assert t.is_subscription
        elif cls is FailureClass.MANDATE_REVOKED:
            assert t.mandate_status is MandateStatus.REVOKED
            assert t.method is PaymentMethod.EMANDATE


def test_card_failures_are_on_cards(batch):
    card_only = {
        FailureClass.CARD_EXPIRED,
        FailureClass.THREE_DS_TIMEOUT,
        FailureClass.SUSPECTED_FRAUD,
    }
    for t in batch.transactions:
        if batch.true_class(t.txn_id) in card_only:
            assert t.method is PaymentMethod.CARD


def test_amounts_are_positive_and_two_dp(batch):
    for t in batch.transactions:
        assert t.amount > 0
        assert t.amount == t.amount.quantize(Decimal("0.01"))


def test_timestamps_span_quiet_and_waking_hours(batch):
    """Quiet hours (21:00-09:00 IST) must be reachable, or that guardrail can
    never fire in the demo."""
    hours = {t.timestamp.hour for t in batch.transactions}
    assert len(hours) >= 20


# --- Planted edge cases ------------------------------------------------------


def test_high_value_transactions_exist_for_the_approval_gate(batch):
    over = [t for t in batch.transactions if t.amount > Decimal("50000")]
    assert len(over) >= 3, "the Rs.50k gate needs more than one row to fire on"


def test_retry_exhausted_rows_exist(batch):
    assert any(t.retry_count >= 2 for t in batch.transactions)


def test_chargeback_rows_exist(batch):
    assert sum(t.chargeback_open for t in batch.transactions) >= 2


def test_opted_out_customers_exist_on_recoverable_failures(batch):
    recoverable = {
        FailureClass.CHECKOUT_ABANDONED,
        FailureClass.THREE_DS_TIMEOUT,
        FailureClass.INSUFFICIENT_FUNDS,
    }
    opted_out = [
        t
        for t in batch.transactions
        if t.opted_out and batch.true_class(t.txn_id) in recoverable
    ]
    assert opted_out, "no opted-out row on a recoverable failure to demo suppression"


def test_repeat_offender_has_four_failures(batch):
    repeat = [t for t in batch.transactions if t.customer_id == "cust_repeat_01"]
    assert len(repeat) == 4
    assert all(not t.opted_out for t in repeat)
    assert all(t.channel_prefs for t in repeat)


def test_fraud_row_is_high_value(batch):
    """The naive-retry-all contrast is sharpest on money."""
    fraud = [
        t
        for t in batch.transactions
        if batch.true_class(t.txn_id) is FailureClass.SUSPECTED_FRAUD
    ]
    assert any(t.amount > Decimal("10000") for t in fraud)


def test_some_customers_are_uncontactable(batch):
    """Empty channel_prefs must occur, so outbound suppression is exercised."""
    assert any(not t.channel_prefs for t in batch.transactions)


def test_salary_day_is_sometimes_unknown(batch):
    """Both the RETRY_SALARY_ALIGNED path and its T+72h fallback need coverage."""
    known = sum(t.salary_day is not None for t in batch.transactions)
    assert 0 < known < 500

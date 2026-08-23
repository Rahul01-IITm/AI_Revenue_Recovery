"""Simulator Column B: lift composes with Column A and matches its spec."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.schemas import Transaction
from core.taxonomy import Action, Channel, FailureClass, MandateStatus, PaymentMethod
from simulate.natural import P_NATURAL_RECOVERY
from simulate.outcome import (
    HUMAN_REVIEW_COST,
    LIFT,
    attempt_succeeds,
    recovery_probability,
    timing_multiplier,
)
from tests.test_natural import section

NOW = datetime(2026, 8, 20, 6, 30, tzinfo=UTC)


def _txn(**overrides) -> Transaction:
    base = dict(
        txn_id="txn_test", customer_id="c", merchant_id="m",
        amount=Decimal("499.00"), timestamp=NOW, method=PaymentMethod.CARD,
        failure_code="GW_51", failure_message="x", retry_count=0,
        customer_ltv=Decimal("0"), prior_success_rate=0.9, prior_failures_7d=0,
        is_subscription=True, mandate_status=MandateStatus.NONE,
        opted_out=False, channel_prefs=(Channel.EMAIL,),
    )
    return Transaction(**{**base, **overrides})


def _parse_column_b() -> dict[tuple[str, str], float]:
    pattern = re.compile(
        r"\|\s*`([A-Z0-9_]+)`\s*\|\s*`([A-Z_]+)`\s*\|\s*\*\*([0-9.]+)\*\*"
    )
    return {
        (m.group(1), m.group(2)): float(m.group(3))
        for m in pattern.finditer(section("## Column B"))
    }


def test_code_matches_the_frozen_column_b_document():
    """Same guarantee as Column A: the document is the spec."""
    documented = _parse_column_b()
    assert documented, "could not parse Column B out of SIMULATION_ASSUMPTIONS.md"

    in_code = {
        (cls.value, action.value): lift
        for cls, actions in LIFT.items()
        for action, lift in actions.items()
    }
    assert documented == in_code, (
        "SIMULATION_ASSUMPTIONS.md and simulate/outcome.py disagree. "
        "Change both together and note it in the change log, or change neither."
    )


def test_absent_pairs_have_zero_lift():
    """Silence is not permission: an action nobody reasoned about earns nothing."""
    p = recovery_probability(
        FailureClass.ISSUER_DOWN, Action.OFFER_PARTIAL_PLAN, NOW, NOW
    )
    assert p == pytest.approx(P_NATURAL_RECOVERY[FailureClass.ISSUER_DOWN])


def test_lift_composes_rather_than_replaces():
    """A retry on a transaction that would have recovered anyway must not be
    counted as recovered *because of* the retry."""
    natural = P_NATURAL_RECOVERY[FailureClass.ISSUER_DOWN]
    p = recovery_probability(
        FailureClass.ISSUER_DOWN, Action.RETRY, NOW + timedelta(hours=2), NOW
    )
    assert p > natural
    assert p == pytest.approx(natural + (1 - natural) * 0.55)


def test_probabilities_stay_in_range():
    for cls in FailureClass:
        for action in Action:
            p = recovery_probability(cls, action, NOW + timedelta(hours=2), NOW)
            assert 0.0 <= p <= 1.0, (cls, action)


def test_terminal_classes_recover_from_nothing():
    for cls in (FailureClass.SUSPECTED_FRAUD, FailureClass.INVALID_ACCOUNT):
        for action in Action:
            assert recovery_probability(cls, action, NOW, NOW) == 0.0


def test_retry_is_worthless_on_classes_only_the_customer_can_fix():
    """CARD_EXPIRED and the mandate classes have hard-zero retry lift. This is
    what makes naive-retry-all waste its attempts."""
    for cls in (
        FailureClass.CARD_EXPIRED,
        FailureClass.MANDATE_EXPIRED,
        FailureClass.MANDATE_REVOKED,
    ):
        assert LIFT[cls][Action.RETRY] == 0.0


def test_salary_aligned_retry_beats_an_immediate_one():
    """The agent's headline claim, asserted rather than asserted-in-a-slide."""
    aligned = LIFT[FailureClass.INSUFFICIENT_FUNDS][Action.RETRY_SALARY_ALIGNED]
    immediate = LIFT[FailureClass.INSUFFICIENT_FUNDS][Action.RETRY]
    assert aligned > immediate * 5


# --- Timing ------------------------------------------------------------------


def test_instant_retry_on_an_outage_is_penalised():
    """Naive-retry-all retries immediately; the outage has not cleared."""
    assert timing_multiplier(
        FailureClass.ISSUER_DOWN, Action.RETRY, NOW, NOW
    ) == 0.40
    assert timing_multiplier(
        FailureClass.ISSUER_DOWN, Action.RETRY, NOW + timedelta(hours=2), NOW
    ) == 1.00


def test_warm_leads_cool():
    early = timing_multiplier(
        FailureClass.THREE_DS_TIMEOUT, Action.SEND_PAYMENT_LINK,
        NOW + timedelta(minutes=30), NOW,
    )
    late = timing_multiplier(
        FailureClass.THREE_DS_TIMEOUT, Action.SEND_PAYMENT_LINK,
        NOW + timedelta(hours=6), NOW,
    )
    assert early > late


def test_timing_changes_the_outcome_probability():
    fast = recovery_probability(
        FailureClass.ISSUER_DOWN, Action.RETRY, NOW + timedelta(hours=2), NOW
    )
    instant = recovery_probability(
        FailureClass.ISSUER_DOWN, Action.RETRY, NOW, NOW
    )
    assert fast > instant


# --- Draw mechanics ----------------------------------------------------------


def test_draws_are_stable_and_order_independent():
    txn = _txn()
    args = (txn, FailureClass.ISSUER_DOWN, Action.RETRY, NOW + timedelta(hours=2))
    a = attempt_succeeds(*args, seed=42, attempt_no=1)
    b = attempt_succeeds(*args, seed=42, attempt_no=1)
    assert a == b


def test_different_rungs_draw_independently():
    """Rung 2 must not inherit rung 1's luck, or a second attempt is pointless."""
    txn = _txn()
    outcomes = {
        attempt_succeeds(
            txn, FailureClass.ISSUER_DOWN, Action.RETRY,
            NOW + timedelta(hours=2), seed=42, attempt_no=n,
        )
        for n in range(1, 20)
    }
    assert len(outcomes) == 2, "all rungs produced the same result"


# --- Cost --------------------------------------------------------------------


def test_human_escalation_is_not_free():
    """Pretending it were would make the Rs.50k gate look costless."""
    assert HUMAN_REVIEW_COST > Decimal("0")

"""The do-nothing floor, and the guarantees that keep it honest."""

from __future__ import annotations

from decimal import Decimal

import pytest

from data.generator import generate_batch
from core.taxonomy import FailureClass
from report.baselines import do_nothing, naive_retry_all
from report.metrics import render, rupees


@pytest.fixture(scope="module")
def batch():
    return generate_batch(n=500, seed=42)


def test_baseline_is_reproducible(batch):
    a = do_nothing(batch, split="test")
    b = do_nothing(batch, split="test")
    assert a.model_dump() == b.model_dump()


def test_baseline_reports_the_split_it_used(batch):
    """A number in the deck must carry provenance."""
    assert do_nothing(batch, split="test").split == "test"
    assert do_nothing(batch, split="train").split == "train"
    assert do_nothing(batch, split=None).split is None


def test_test_and_train_are_disjoint_and_sum_to_all(batch):
    test = do_nothing(batch, split="test")
    train = do_nothing(batch, split="train")
    everything = do_nothing(batch, split=None)

    assert test.count + train.count == everything.count
    assert test.at_risk + train.at_risk == everything.at_risk
    assert (
        test.recovered_amount + train.recovered_amount
        == everything.recovered_amount
    )


def test_recovery_rate_is_plausible(batch):
    """~13% by count per the frozen assumptions. Anything wildly outside means
    the mix or the simulator drifted."""
    result = do_nothing(batch, split=None)
    assert 0.05 < result.recovery_rate_count < 0.25


def test_recovered_never_exceeds_at_risk(batch):
    result = do_nothing(batch, split=None)
    assert result.recovered_amount <= result.at_risk
    assert result.recovered_count <= result.count


def test_terminal_classes_recover_nothing(batch):
    """Fraud and invalid accounts must contribute zero to the floor."""
    result = do_nothing(batch, split=None)
    for b in result.by_class:
        if b.failure_class in (
            FailureClass.SUSPECTED_FRAUD,
            FailureClass.INVALID_ACCOUNT,
        ):
            assert b.recovered_count == 0
            assert b.recovered_amount == Decimal("0")


def test_do_nothing_costs_nothing(batch):
    """No messages sent, so net recovery equals gross."""
    result = do_nothing(batch, split=None)
    assert result.intervention_cost == Decimal("0")
    assert result.net_recovered == result.recovered_amount


def test_breakdown_totals_match_the_headline(batch):
    result = do_nothing(batch, split="test")
    assert sum(b.count for b in result.by_class) == result.count
    assert sum(b.at_risk for b in result.by_class) == result.at_risk
    assert (
        sum(b.recovered_amount for b in result.by_class) == result.recovered_amount
    )


def test_naive_baseline_racks_up_violations(batch):
    """The honest competitor, and the contrast that sells the policy engine:
    an unguarded retry loop retries fraud and messages opted-out customers."""
    result = naive_retry_all(batch, split=None)
    assert result.violations, "naive should violate something; that is the point"
    assert result.violations.get("retried_suspected_fraud", 0) > 0
    assert result.violations.get("messaged_opted_out_customer", 0) > 0


def test_agent_commits_no_violations(batch):
    """The same counters, applied to the agent, must all be zero."""
    from runner import run_agent

    result, _ = run_agent(batch, split=None)
    assert result.violations == {}
    assert result.max_contacts_per_customer <= 3


# --- Presentation ------------------------------------------------------------


def test_rupees_formatting():
    assert rupees(Decimal("4100")) == "Rs.4,100"
    assert rupees(Decimal("410000")) == "Rs.4.10L"
    assert rupees(Decimal("41000000")) == "Rs.4.10Cr"


def test_render_shows_split_and_headline(batch):
    out = render(do_nothing(batch, split="test"))
    assert "test" in out
    assert "Total at risk" in out
    assert "By failure class" in out

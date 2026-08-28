"""Escalation budget and queue priority.

Human review is the one intervention the agent cannot buy more of. These tests
cover the cap, and the property that makes a cap acceptable: when capacity runs
out, it ran out on the *least* valuable transactions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.schemas import PlannedAction, RecoverabilityAssessment, Transaction
from core.taxonomy import (
    Action,
    Channel,
    FailureClass,
    MandateStatus,
    PaymentMethod,
    Recoverability,
    Verdict,
)
from data.generator import generate_batch
from plan.priority import (
    LTV_CHURN_WEIGHT,
    escalation_priority,
    order_queue,
    value_at_stake,
)
from policy.engine import PolicyEngine
from policy.limits import MAX_HUMAN_ESCALATIONS_PER_RUN
from policy.state import RunState
from runner import run_agent

NOW = datetime(2026, 8, 20, 6, 30, tzinfo=UTC)


def _txn(**overrides) -> Transaction:
    base = dict(
        txn_id="txn_test", customer_id="c", merchant_id="m",
        amount=Decimal("499.00"), timestamp=NOW, method=PaymentMethod.CARD,
        failure_code="GW_91", failure_message="issuer unavailable, please retry",
        retry_count=0, customer_ltv=Decimal("0"), prior_success_rate=0.9,
        prior_failures_7d=0, is_subscription=True,
        mandate_status=MandateStatus.NONE, opted_out=False,
        channel_prefs=(Channel.EMAIL,),
    )
    return Transaction(**{**base, **overrides})


def _score(score: float = 0.7) -> RecoverabilityAssessment:
    return RecoverabilityAssessment(
        txn_id="txn_test", score=score, tier=Recoverability.HIGH, confidence=0.95
    )


# --- The budget --------------------------------------------------------------


def test_escalation_is_allowed_while_budget_remains():
    engine, state = PolicyEngine(), RunState()
    txn = _txn()
    plan = PlannedAction(txn_id=txn.txn_id, action=Action.ESCALATE_HUMAN,
                         scheduled_at=NOW, rationale="t")
    d = engine.evaluate(txn, plan, state, NOW, FailureClass.ISSUER_DOWN)
    assert d.verdict is Verdict.ALLOW
    assert d.final_action is Action.ESCALATE_HUMAN


def test_escalation_is_refused_once_the_budget_is_spent():
    engine, state = PolicyEngine(), RunState()
    txn = _txn()
    for _ in range(MAX_HUMAN_ESCALATIONS_PER_RUN):
        state.record(txn, Action.ESCALATE_HUMAN, NOW)

    plan = PlannedAction(txn_id=txn.txn_id, action=Action.ESCALATE_HUMAN,
                         scheduled_at=NOW, rationale="t")
    d = engine.evaluate(txn, plan, state, NOW, FailureClass.ISSUER_DOWN)
    assert d.verdict is Verdict.VETO
    assert d.final_action is Action.STOP
    assert d.rule_id == "escalation_budget"
    assert "no reviewer available" in d.reason


def test_budget_also_catches_downgraded_escalations():
    """A Rs.1.2L retry becomes an escalation. It must still hit the budget —
    otherwise the approval gate becomes an unbounded spending channel."""
    engine, state = PolicyEngine(), RunState()
    txn = _txn(amount=Decimal("120000.00"))
    for _ in range(MAX_HUMAN_ESCALATIONS_PER_RUN):
        state.record(txn, Action.ESCALATE_HUMAN, NOW)

    plan = PlannedAction(txn_id=txn.txn_id, action=Action.RETRY,
                         scheduled_at=NOW, rationale="t")
    d = engine.evaluate(txn, plan, state, NOW, FailureClass.ISSUER_DOWN)
    assert d.final_action is Action.STOP
    assert d.rule_id == "escalation_budget"


def test_budget_does_not_block_other_actions():
    engine, state = PolicyEngine(), RunState()
    txn = _txn()
    for _ in range(MAX_HUMAN_ESCALATIONS_PER_RUN * 2):
        state.record(txn, Action.ESCALATE_HUMAN, NOW)

    plan = PlannedAction(txn_id=txn.txn_id, action=Action.RETRY,
                         scheduled_at=NOW, rationale="t")
    d = engine.evaluate(txn, plan, state, NOW, FailureClass.ISSUER_DOWN)
    assert d.verdict is Verdict.ALLOW


def test_agent_never_exceeds_the_escalation_budget():
    for n in (500, 2000):
        batch = generate_batch(n=n, seed=3)
        result, _ = run_agent(batch, split=None)
        assert result.escalated_count <= MAX_HUMAN_ESCALATIONS_PER_RUN, n


def test_budget_actually_binds_at_scale():
    """A cap nobody ever reaches is not a guardrail."""
    batch = generate_batch(n=2000, seed=3)
    result, _ = run_agent(batch, split=None)
    assert result.escalated_count == MAX_HUMAN_ESCALATIONS_PER_RUN
    assert result.stopped_reasons.get("escalation_budget", 0) > 0


# --- Priority ----------------------------------------------------------------


def test_lifetime_value_raises_what_is_at_stake():
    """The field exists to be used. A failed payment from a high-LTV customer
    risks more than the payment."""
    plain = value_at_stake(_txn(customer_ltv=Decimal("0")))
    loyal = value_at_stake(_txn(customer_ltv=Decimal("24000")))
    assert loyal > plain
    assert loyal == Decimal("499") + LTV_CHURN_WEIGHT * Decimal("24000")


def test_amount_still_dominates_lifetime_value():
    """LTV is a tiebreaker, not the driver. A Rs.12k failure outranks a Rs.499
    failure from a high-LTV customer."""
    big = value_at_stake(_txn(amount=Decimal("11999"), customer_ltv=Decimal("0")))
    loyal_small = value_at_stake(
        _txn(amount=Decimal("499"), customer_ltv=Decimal("24000"))
    )
    assert big > loyal_small


def test_priority_combines_value_and_recoverability():
    """Neither a large hopeless transaction nor a winnable trivial one should
    consume a reviewer."""
    hopeless_big = escalation_priority(_txn(amount=Decimal("50000")), _score(0.02))
    winnable_small = escalation_priority(_txn(amount=Decimal("149")), _score(0.9))
    solid = escalation_priority(_txn(amount=Decimal("9999")), _score(0.7))
    assert solid > hopeless_big
    assert solid > winnable_small


def test_queue_is_ordered_highest_value_first():
    pairs = [
        (_txn(txn_id="small", amount=Decimal("199")), _score(0.7)),
        (_txn(txn_id="large", amount=Decimal("11999")), _score(0.7)),
        (_txn(txn_id="mid", amount=Decimal("2999")), _score(0.7)),
    ]
    assert [t.txn_id for t, _ in order_queue(pairs)] == ["large", "mid", "small"]


def test_queue_order_is_deterministic():
    pairs = [
        (_txn(txn_id=f"txn_{i}", amount=Decimal("499")), _score(0.5))
        for i in range(20)
    ]
    assert order_queue(pairs) == order_queue(list(reversed(pairs)))


def test_scarce_capacity_goes_to_the_most_valuable_transactions():
    """The property that makes a hard cap defensible: when reviewers run out,
    they ran out on the cheapest transactions, not on an arbitrary tail."""
    batch = generate_batch(n=2000, seed=3)
    result, store = run_agent(batch, split=None)
    assert result.stopped_reasons.get("escalation_budget", 0) > 0

    escalated = {
        row["txn_id"]
        for row in store.conn.execute(
            "SELECT txn_id FROM decisions WHERE final_action = 'ESCALATE_HUMAN'"
        ).fetchall()
        for row in [{"txn_id": row[0]}]
    }
    refused = {
        row[0]
        for row in store.conn.execute(
            "SELECT txn_id FROM decisions WHERE rule_id = 'escalation_budget'"
        ).fetchall()
    }
    amounts = {t.txn_id: t.amount for t in batch.transactions}
    assert escalated and refused
    cheapest_served = min(amounts[t] for t in escalated)
    dearest_refused = max(amounts[t] for t in refused)
    # Some overlap is possible because priority weighs recoverability too, but
    # the served set must be clearly richer than the refused set.
    served_mean = sum(amounts[t] for t in escalated) / len(escalated)
    refused_mean = sum(amounts[t] for t in refused) / len(refused)
    assert served_mean > refused_mean


# --- The replay-cost bug this work uncovered --------------------------------


def test_replayed_actions_are_not_charged():
    """The ladder can propose ESCALATE_HUMAN twice for one transaction. The
    idempotency key catches the duplicate — but the runner was still counting
    it and charging HUMAN_REVIEW_COST for a reviewer who was never queued."""
    batch = generate_batch(n=500, seed=3)
    result, store = run_agent(batch, split=None)

    distinct = store.conn.execute(
        "SELECT COUNT(DISTINCT txn_id) FROM executions "
        "WHERE action = 'ESCALATE_HUMAN' AND replayed = 0"
    ).fetchone()[0]
    assert result.escalated_count == distinct, (
        "escalated_count counts replays that never queued a reviewer"
    )

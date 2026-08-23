"""Executor, idempotency, and the audit store's append-only guarantee."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from audit.store import AuditStore
from core.schemas import Outcome, PlannedAction, Transaction
from core.taxonomy import Action, Channel, FailureClass, MandateStatus, PaymentMethod, Verdict
from execute.executor import MAX_API_ATTEMPTS, Executor
from execute.idempotency import idempotency_key
from execute.razorpay_adapter import ApiResponse, RazorpayAdapter
from policy.engine import PolicyEngine
from policy.state import RunState

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


def _decide(txn, action, cls=FailureClass.ISSUER_DOWN, state=None):
    engine = PolicyEngine()
    plan = PlannedAction(txn_id=txn.txn_id, action=action,
                         scheduled_at=NOW + timedelta(hours=2), rationale="t")
    return engine.evaluate(txn, plan, state or RunState(), NOW, cls)


# --- Idempotency -------------------------------------------------------------


def test_key_is_stable_for_the_same_inputs():
    a = idempotency_key("run1", "txn_1", "RETRY", 1)
    b = idempotency_key("run1", "txn_1", "RETRY", 1)
    assert a == b


def test_key_changes_with_every_component():
    base = idempotency_key("run1", "txn_1", "RETRY", 1)
    assert idempotency_key("run2", "txn_1", "RETRY", 1) != base
    assert idempotency_key("run1", "txn_2", "RETRY", 1) != base
    assert idempotency_key("run1", "txn_1", "NUDGE_EMAIL", 1) != base
    assert idempotency_key("run1", "txn_1", "RETRY", 2) != base


def test_replay_is_a_no_op_and_costs_nothing():
    """The duplicate-run drill: nobody gets charged or messaged twice."""
    txn = _txn()
    state = RunState()
    ex = Executor(run_id="run1")
    decision = _decide(txn, Action.RETRY)

    first = ex.execute(txn, decision, state, NOW)
    assert first.ok and not first.replayed

    second = ex.execute(txn, decision, state, NOW)
    assert second.replayed
    assert second.cost == Decimal("0")


def test_replay_does_not_consume_retry_budget():
    txn = _txn()
    state = RunState()
    ex = Executor(run_id="run1")
    decision = _decide(txn, Action.RETRY)

    ex.execute(txn, decision, state, NOW)
    used_after_first = state.retries_used(txn)
    ex.execute(txn, decision, state, NOW)
    assert state.retries_used(txn) == used_after_first


# --- Authorisation is required ------------------------------------------------


def test_vetoed_decisions_are_never_executed():
    txn = _txn(opted_out=True)
    decision = _decide(txn, Action.NUDGE_WHATSAPP, FailureClass.CHECKOUT_ABANDONED)
    assert decision.verdict is Verdict.VETO

    result = Executor().execute(txn, decision, RunState(), NOW)
    assert result.ok is False
    assert result.cost == Decimal("0")
    assert "opt_out" in result.detail


def test_stop_is_never_executed():
    txn = _txn()
    decision = _decide(txn, Action.STOP)
    result = Executor().execute(txn, decision, RunState(), NOW)
    assert result.ok is False


def test_fraud_retry_is_blocked_end_to_end():
    """The sharpest contrast in the deck: naive retry-all would charge this."""
    txn = _txn(amount=Decimal("18999.00"))
    decision = _decide(txn, Action.RETRY, FailureClass.SUSPECTED_FRAUD)
    result = Executor().execute(txn, decision, RunState(), NOW)
    assert result.ok is False
    assert result.action is Action.STOP


# --- Costs -------------------------------------------------------------------


def test_messages_cost_money_and_retries_do_not():
    txn = _txn()
    state = RunState()
    ex = Executor()

    msg = ex.execute(txn, _decide(txn, Action.NUDGE_WHATSAPP,
                                  FailureClass.CHECKOUT_ABANDONED), state, NOW)
    assert msg.cost > Decimal("0")
    assert msg.channel is Channel.WHATSAPP

    retry = ex.execute(txn, _decide(txn, Action.RETRY), state, NOW)
    assert retry.cost == Decimal("0")


def test_channel_falls_back_to_what_the_customer_accepts():
    txn = _txn(channel_prefs=(Channel.EMAIL,))
    result = Executor().execute(
        txn, _decide(txn, Action.NUDGE_WHATSAPP, FailureClass.CHECKOUT_ABANDONED),
        RunState(), NOW,
    )
    assert result.channel is Channel.EMAIL


# --- API failure handling ----------------------------------------------------


class _FlakyAdapter(RazorpayAdapter):
    def __init__(self, fail_times: int):
        super().__init__(key_id=None, key_secret=None)
        self.calls = 0
        self.fail_times = fail_times

    def retry_payment(self, txn_id, amount):
        self.calls += 1
        if self.calls <= self.fail_times:
            return ApiResponse(ok=False, reference="", mode="test", detail="503")
        return ApiResponse(ok=True, reference="ok", mode="test")


def test_transient_api_failure_is_retried_with_backoff():
    adapter = _FlakyAdapter(fail_times=1)
    ex = Executor(adapter=adapter, sleep=lambda _: None)
    result = ex.execute(_txn(), _decide(_txn(), Action.RETRY), RunState(), NOW)
    assert result.ok
    assert adapter.calls == 2


def test_persistent_api_failure_is_quarantined_not_raised():
    """One bad endpoint must not abort a 500-transaction batch."""
    adapter = _FlakyAdapter(fail_times=99)
    ex = Executor(adapter=adapter, sleep=lambda _: None)
    result = ex.execute(_txn(), _decide(_txn(), Action.RETRY), RunState(), NOW)
    assert result.ok is False
    assert adapter.calls == MAX_API_ATTEMPTS


def test_adapter_runs_offline_without_credentials():
    adapter = RazorpayAdapter(key_id=None, key_secret=None)
    assert adapter.live is False
    assert adapter.mode == "offline"
    assert adapter.retry_payment("txn_1", Decimal("100")).mode == "offline"


# --- Audit store -------------------------------------------------------------


@pytest.fixture
def store():
    return AuditStore(":memory:")


def test_decisions_are_recorded_with_an_input_snapshot(store):
    txn = _txn()
    decision = _decide(txn, Action.RETRY)
    store.record_decision("run1", decision, txn)
    store.commit()

    rows = store.decisions_for(txn.txn_id)
    assert len(rows) == 1
    assert rows[0]["rule_id"] == decision.rule_id
    assert txn.txn_id in rows[0]["input_snapshot"]


@pytest.mark.parametrize("table", ["decisions", "executions", "outcomes"])
def test_audit_tables_reject_updates_and_deletes(store, table):
    """Append-only enforced by the database, not by convention."""
    txn = _txn()
    store.record_decision("run1", _decide(txn, Action.RETRY), txn)
    store.record_execution("run1", Executor().execute(
        txn, _decide(txn, Action.RETRY), RunState(), NOW))
    store.record_outcome("run1", Outcome(
        txn_id=txn.txn_id, recovered=True, amount=Decimal("499")), NOW)
    store.commit()

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute(f"UPDATE {table} SET txn_id = 'hacked'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute(f"DELETE FROM {table}")


def test_blocked_counts_group_by_rule(store):
    for i in range(3):
        txn = _txn(txn_id=f"txn_{i}", opted_out=True)
        store.record_decision(
            "run1",
            _decide(txn, Action.NUDGE_EMAIL, FailureClass.CHECKOUT_ABANDONED),
            txn,
        )
    store.commit()
    counts = dict(store.blocked_counts("run1"))
    assert counts.get("opt_out") == 3


def test_a_decision_can_be_reconstructed_after_the_fact(store):
    txn = _txn()
    decision = _decide(txn, Action.RETRY)
    store.record_decision("run1", decision, txn,
                          diagnosis_json='{"failure_class":"ISSUER_DOWN",'
                                         '"confidence":0.95,"rule_id":"code:GW_91"}')
    store.record_execution("run1", Executor().execute(txn, decision, RunState(), NOW))
    store.commit()

    text = store.reconstruct(txn.txn_id)
    assert "ISSUER_DOWN" in text
    assert "RETRY" in text
    assert decision.rule_id in text

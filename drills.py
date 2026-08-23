"""Failure drills — the guardrails, demonstrated end to end on the real batch.

Each drill runs the full pipeline against the planted edge cases and returns
evidence. They are executable rather than described, so the demo shows the
system doing it rather than a slide claiming it does.

    python run_batch.py --mode drills

`tests/test_drills.py` asserts every drill passes, so a regression that breaks
a guardrail fails the suite rather than surfacing on stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from audit.store import AuditStore
from core.schemas import Batch, PlannedAction, Transaction
from core.taxonomy import Action, FailureClass, Verdict
from data.generator import generate_batch
from diagnose.classifier import classify
from execute.executor import Executor
from execute.razorpay_adapter import ApiResponse, RazorpayAdapter
from policy.engine import PolicyEngine
from policy.limits import HUMAN_APPROVAL_THRESHOLD, MAX_RETRIES_PER_TXN
from policy.state import RunState
from runner import run_agent, run_naive


@dataclass
class DrillResult:
    name: str
    passed: bool
    evidence: str
    why_it_matters: str


def _find(batch: Batch, predicate) -> Transaction:
    return next(t for t in batch.transactions if predicate(t))


def _decide(batch: Batch, txn: Transaction, action: Action, state=None, at=None):
    engine = PolicyEngine()
    now = at or batch.generated_at
    planned = PlannedAction(
        txn_id=txn.txn_id, action=action, scheduled_at=now, rationale="drill"
    )
    return engine.evaluate(
        txn, planned, state or RunState(), now, classify(txn).failure_class
    )


def _find_retryable(batch: Batch) -> Transaction:
    """A transaction the policy engine actually authorises a RETRY on.

    The executor drills measure execution behaviour, so they need a row that
    reaches the executor. Picking one by transaction attributes alone is not
    enough — the engine may still veto it for a reason unrelated to the drill,
    and the drill would then silently measure nothing.
    """
    for txn in batch.transactions:
        decision = _decide(batch, txn, Action.RETRY)
        if decision.verdict is Verdict.ALLOW and decision.final_action is Action.RETRY:
            return txn
    raise AssertionError("no retryable transaction in the batch")


# --- Drills ------------------------------------------------------------------


def drill_retry_cap(batch: Batch) -> DrillResult:
    txn = _find(batch, lambda t: t.retry_count >= MAX_RETRIES_PER_TXN)
    d = _decide(batch, txn, Action.RETRY)
    return DrillResult(
        "Retry cap exceeded",
        d.verdict is Verdict.VETO and d.final_action is Action.STOP,
        f"{txn.txn_id} has {txn.retry_count} prior retries -> "
        f"{d.verdict.value}, action {d.final_action.value}, rule '{d.rule_id}'",
        "The agent gives up, and logs why. Without this it retries forever.",
    )


def drill_approval_threshold(batch: Batch) -> DrillResult:
    txn = _find(batch, lambda t: t.amount >= HUMAN_APPROVAL_THRESHOLD)
    d = _decide(batch, txn, Action.RETRY)
    return DrillResult(
        "Rs.50k human-approval gate",
        d.verdict is Verdict.DOWNGRADE and d.final_action is Action.ESCALATE_HUMAN,
        f"{txn.txn_id} is Rs.{txn.amount:,.0f} -> {d.verdict.value} to "
        f"{d.final_action.value}, rule '{d.rule_id}'",
        "Money above the threshold gets a human owner, not an autonomous agent.",
    )


def drill_fraud_hard_block(batch: Batch) -> DrillResult:
    txn = _find(
        batch,
        lambda t: batch.true_class(t.txn_id) is FailureClass.SUSPECTED_FRAUD,
    )
    d = _decide(batch, txn, Action.RETRY)
    naive = run_naive(batch, split=None)
    would_have = naive.violations.get("retried_suspected_fraud", 0)
    return DrillResult(
        "Fraud is never retried",
        d.verdict is Verdict.VETO and would_have > 0,
        f"{txn.txn_id} (Rs.{txn.amount:,.0f}) -> {d.verdict.value} by "
        f"'{d.rule_id}'. Naive retry-all would have retried fraud "
        f"{would_have} times.",
        "Retrying suspected fraud is a compliance incident, not a lost sale.",
    )


def drill_opt_out(batch: Batch) -> DrillResult:
    txn = _find(batch, lambda t: t.opted_out and t.channel_prefs)
    d = _decide(batch, txn, Action.NUDGE_WHATSAPP)
    naive = run_naive(batch, split=None)
    return DrillResult(
        "Opt-out suppresses all outbound",
        d.verdict is Verdict.VETO
        and naive.violations.get("messaged_opted_out_customer", 0) > 0,
        f"{txn.txn_id} customer opted out -> {d.verdict.value} by '{d.rule_id}'. "
        f"Naive messaged opted-out customers "
        f"{naive.violations.get('messaged_opted_out_customer', 0)} times.",
        "Opt-out is absolute and checked before every outbound action.",
    )


def drill_chargeback_block(batch: Batch) -> DrillResult:
    txn = _find(batch, lambda t: t.chargeback_open)
    d = _decide(batch, txn, Action.RETRY)
    return DrillResult(
        "Open chargeback blocks everything",
        d.verdict is Verdict.VETO,
        f"{txn.txn_id} has an open dispute -> {d.verdict.value} by '{d.rule_id}'",
        "Touching a payment while a dispute is live is indefensible.",
    )


def drill_recovery_window(batch: Batch) -> DrillResult:
    late = batch.generated_at + timedelta(hours=80)
    txn = _find(batch, lambda t: not t.chargeback_open)
    d = _decide(batch, txn, Action.RETRY, at=late)
    return DrillResult(
        "72h recovery window closes",
        d.verdict is Verdict.VETO and d.rule_id == "recovery_window",
        f"Action scheduled 80h after failure -> {d.verdict.value} by '{d.rule_id}'",
        "A stopping rule the agent applies to itself.",
    )


def drill_message_cap(batch: Batch) -> DrillResult:
    result, _ = run_agent(batch, split=None)
    naive = run_naive(batch, split=None)
    return DrillResult(
        "Anti-spam message cap",
        result.max_contacts_per_customer <= 3,
        f"Agent: max {result.max_contacts_per_customer} messages per customer "
        f"across {result.contacts_sent} total. "
        f"Naive: max {naive.max_contacts_per_customer}.",
        "A customer with four failures does not get four dunning sequences.",
    )


def drill_idempotency(batch: Batch) -> DrillResult:
    txn = _find_retryable(batch)
    state = RunState()
    ex = Executor(run_id="drill")
    d = _decide(batch, txn, Action.RETRY, state=state)

    first = ex.execute(txn, d, state, batch.generated_at)
    second = ex.execute(txn, d, state, batch.generated_at)
    return DrillResult(
        "Duplicate run is a no-op",
        first.ok and not first.replayed and second.replayed and second.cost == 0,
        f"{txn.txn_id} executed once (key {first.idempotency_key[:12]}...), "
        f"replay detected, cost Rs.{second.cost}",
        "Re-running the batch cannot charge or message anyone twice.",
    )


def drill_executor_quarantine(batch: Batch) -> DrillResult:
    class _Dead(RazorpayAdapter):
        def __init__(self):
            super().__init__(key_id=None, key_secret=None)
            self.calls = 0

        def retry_payment(self, txn_id, amount):
            self.calls += 1
            return ApiResponse(ok=False, reference="", mode="test", detail="503")

    adapter = _Dead()
    ex = Executor(adapter=adapter, sleep=lambda _: None)
    txn = _find_retryable(batch)
    result = ex.execute(
        txn, _decide(batch, txn, Action.RETRY), RunState(), batch.generated_at
    )
    return DrillResult(
        "Executor API failure is quarantined",
        not result.ok and adapter.calls == 3,
        f"Endpoint down: {adapter.calls} attempts with backoff, then quarantined. "
        "No exception escaped; the batch continues.",
        "One flaky endpoint must not abort a 500-transaction run.",
    )


def drill_llm_degradation(batch: Batch) -> DrillResult:
    from diagnose.llm_classifier import LlmClassifier

    class _Broken:
        def __init__(self):
            self.calls = 0

            class _M:
                @staticmethod
                def parse(**kwargs):
                    raise ConnectionError("LLM endpoint unreachable")

            self.messages = _M()

    clf = LlmClassifier(client=_Broken())
    diagnoses = [clf.classify(t) for t in batch.transactions[:60]]
    return DrillResult(
        "LLM outage degrades to rules",
        all(d.source == "rules" for d in diagnoses) and clf.stats.circuit_opened,
        f"{clf.stats.escalated} escalated, all failed, circuit opened after "
        f"{clf.stats.degraded}. Every row still diagnosed by rules.",
        "If the API is down mid-demo, the run completes with slightly lower "
        "diagnosis accuracy and nothing else changes.",
    )


def drill_undiagnosed_escalates(batch: Batch) -> DrillResult:
    txn = _find(batch, lambda t: classify(t).failure_class is None)
    d = _decide(batch, txn, Action.RETRY)
    return DrillResult(
        "Undiagnosed rows go to a human",
        d.final_action is Action.ESCALATE_HUMAN,
        f"{txn.txn_id} code {txn.failure_code!r} unclassifiable -> "
        f"{d.final_action.value} by '{d.rule_id}'",
        "The classifier refuses to guess; the engine honours that refusal.",
    )


ALL_DRILLS = (
    drill_retry_cap,
    drill_approval_threshold,
    drill_fraud_hard_block,
    drill_opt_out,
    drill_chargeback_block,
    drill_recovery_window,
    drill_message_cap,
    drill_idempotency,
    drill_executor_quarantine,
    drill_llm_degradation,
    drill_undiagnosed_escalates,
)


def run_all(batch: Batch | None = None) -> list[DrillResult]:
    batch = batch or generate_batch(n=500, seed=42)
    return [drill(batch) for drill in ALL_DRILLS]


def render(results: list[DrillResult]) -> str:
    lines = ["", "  Failure drills", "  " + "=" * 60, ""]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines += [
            f"  [{mark}] {r.name}",
            f"         {r.evidence}",
            f"         why: {r.why_it_matters}",
            "",
        ]
    passed = sum(r.passed for r in results)
    lines += [f"  {passed}/{len(results)} drills passed", ""]
    return "\n".join(lines)

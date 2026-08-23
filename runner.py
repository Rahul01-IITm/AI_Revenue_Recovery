"""Orchestrates the full pipeline: diagnose -> plan -> POLICY -> execute -> simulate.

Two runners live here, deliberately side by side:

`run_agent`  routes every proposed action through the policy engine.
`run_naive`  does not, because that is what most merchants actually do.

Keeping them adjacent makes the comparison honest and makes the contrast
visible in one file: the naive runner's violation counters exist precisely
because nothing stops it doing the things the agent is forbidden from doing.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from audit.store import AuditStore
from core.schemas import Batch, Outcome, PlannedAction, Split, Transaction
from core.taxonomy import OUTBOUND_ACTIONS, RETRY_ACTIONS, Action, FailureClass, Verdict
from diagnose.classifier import classify
from diagnose.recoverability import assess
from execute.channels import choose_channel, cost_of
from execute.executor import Executor
from plan.planner import plan as plan_action
from policy.engine import PolicyEngine
from policy.limits import HUMAN_APPROVAL_THRESHOLD
from policy.state import RunState
from report.metrics import RunResult, build_breakdown
from simulate.natural import recovers_naturally
from simulate.outcome import HUMAN_REVIEW_COST, attempt_succeeds

MAX_LADDER_RUNGS = 2


def run_agent(
    batch: Batch,
    split: Split | None = "test",
    store: AuditStore | None = None,
    run_id: str = "agent",
    message_writer=None,
    use_llm: bool = False,
) -> tuple[RunResult, AuditStore]:
    """The agent. Every action passes through the policy engine.

    `use_llm` upgrades diagnosis and message copy. It changes nothing structural:
    the LLM is consulted only where the rules were weak, and degrades back to
    them on any failure, so the run completes with the API down.
    """
    llm = None
    if use_llm:
        from diagnose.llm_classifier import LlmClassifier

        llm = LlmClassifier()

    store = store or AuditStore(":memory:")
    engine = PolicyEngine()
    state = RunState()
    executor = Executor(run_id=run_id)
    now = batch.generated_at

    rows, outcomes = [], []
    actions = Counter()
    blocked = Counter()
    stopped = Counter()
    contacts = Counter()
    escalated_count = 0
    escalated_value = Decimal("0")
    total_cost = Decimal("0")

    for txn in batch.select(split):
        cls_truth = batch.true_class(txn.txn_id)
        diagnosis = llm.classify(txn) if llm else classify(txn)
        assessment = assess(txn, diagnosis)

        recovered = False
        recovered_by: Action | None = None

        for rung in range(1, MAX_LADDER_RUNGS + 1):
            planned = plan_action(txn, diagnosis, assessment, now, rung)
            if planned is None:
                break

            decision, may_execute = engine.authorise_or_stop(
                txn, planned, state, now, diagnosis.failure_class
            )
            store.record_decision(
                run_id, decision, txn, diagnosis.model_dump_json()
            )

            if decision.verdict is Verdict.VETO:
                blocked[decision.rule_id] += 1
            if decision.final_action is Action.STOP:
                # A STOP the policy engine merely permitted came from the
                # planner's own stopping rule; attributing it to
                # "allow:default" would hide who actually gave up.
                stopped[
                    decision.rule_id
                    if decision.verdict is not Verdict.ALLOW
                    else "planner:not_worth_chasing"
                ] += 1
                break
            if not may_execute:
                break

            body = message_writer(txn, diagnosis, decision) if message_writer else ""
            result = executor.execute(txn, decision, state, now, body)
            store.record_execution(run_id, result)

            actions[decision.final_action.value] += 1
            cost = result.cost
            if decision.final_action is Action.ESCALATE_HUMAN:
                cost = HUMAN_REVIEW_COST
                escalated_count += 1
                escalated_value += txn.amount
            total_cost += cost
            if decision.final_action in OUTBOUND_ACTIONS:
                contacts[txn.customer_id] += 1

            if not result.ok:
                continue

            if attempt_succeeds(
                txn, cls_truth, decision.final_action,
                decision.final_scheduled_at, seed=batch.seed, attempt_no=rung,
            ):
                recovered = True
                recovered_by = decision.final_action
                break

        counterfactual = recovered and recovers_naturally(
            txn, cls_truth, seed=batch.seed
        )
        outcome = Outcome(
            txn_id=txn.txn_id, recovered=recovered, amount=txn.amount,
            recovered_by=recovered_by, counterfactual=counterfactual,
            cost=Decimal("0"),
        )
        store.record_outcome(run_id, outcome, now)
        outcomes.append(outcome)
        rows.append((cls_truth, txn.amount, recovered))

    store.commit()
    if llm is not None:
        log_llm = (
            f"LLM: {llm.stats.escalated} escalated, {llm.stats.accepted} accepted, "
            f"{llm.stats.degraded} degraded to rules {llm.stats.degradation_reasons}"
        )
        print(f"  {log_llm}")

    return (
        _assemble(
            "agent", split, batch, rows, outcomes, total_cost,
            actions=dict(actions), blocked=dict(blocked), stopped=dict(stopped),
            contacts=contacts, escalated_count=escalated_count,
            escalated_value=escalated_value, violations={},
        ),
        store,
    )


def run_naive(
    batch: Batch, split: Split | None = "test", run_id: str = "naive"
) -> RunResult:
    """Retry everything twice, immediately, and message everyone once.

    The honest competitor: this is what most merchants actually do. It does not
    consult the policy engine, which is the entire point -- the violation
    counters below are what an unguarded system racks up.
    """
    now = batch.generated_at
    rows, outcomes = [], []
    actions = Counter()
    contacts = Counter()
    violations = Counter()
    total_cost = Decimal("0")

    for txn in batch.select(split):
        cls_truth = batch.true_class(txn.txn_id)
        recovered = False
        recovered_by: Action | None = None

        # Two immediate retries, regardless of failure class.
        for rung in (1, 2):
            actions[Action.RETRY.value] += 1
            if cls_truth in (FailureClass.SUSPECTED_FRAUD,):
                violations["retried_suspected_fraud"] += 1
            if cls_truth is FailureClass.INVALID_ACCOUNT:
                violations["retried_invalid_account"] += 1
            if txn.chargeback_open:
                violations["retried_open_chargeback"] += 1
            if txn.retry_count + rung > 2:
                violations["exceeded_retry_cap"] += 1
            if txn.amount >= HUMAN_APPROVAL_THRESHOLD:
                violations["auto_actioned_above_approval_threshold"] += 1

            if not recovered and attempt_succeeds(
                txn, cls_truth, Action.RETRY, txn.timestamp,
                seed=batch.seed, attempt_no=rung,
            ):
                recovered = True
                recovered_by = Action.RETRY

        # One blanket message to anyone with a channel, opt-out ignored.
        if txn.channel_prefs:
            channel = choose_channel(txn, Action.NUDGE_EMAIL)
            if channel:
                actions[Action.NUDGE_EMAIL.value] += 1
                total_cost += cost_of(channel)
                contacts[txn.customer_id] += 1
                if txn.opted_out:
                    violations["messaged_opted_out_customer"] += 1
                if not recovered and attempt_succeeds(
                    txn, cls_truth, Action.NUDGE_EMAIL, txn.timestamp,
                    seed=batch.seed, attempt_no=3,
                ):
                    recovered = True
                    recovered_by = Action.NUDGE_EMAIL

        counterfactual = recovered and recovers_naturally(
            txn, cls_truth, seed=batch.seed
        )
        outcomes.append(Outcome(
            txn_id=txn.txn_id, recovered=recovered, amount=txn.amount,
            recovered_by=recovered_by, counterfactual=counterfactual,
        ))
        rows.append((cls_truth, txn.amount, recovered))

    return _assemble(
        "naive-retry-all", split, batch, rows, outcomes, total_cost,
        actions=dict(actions), blocked={}, stopped={}, contacts=contacts,
        escalated_count=0, escalated_value=Decimal("0"),
        violations=dict(violations),
    )


def _assemble(
    mode: str,
    split: Split | None,
    batch: Batch,
    rows: list,
    outcomes: list[Outcome],
    total_cost: Decimal,
    *,
    actions: dict,
    blocked: dict,
    stopped: dict,
    contacts: Counter,
    escalated_count: int,
    escalated_value: Decimal,
    violations: dict,
) -> RunResult:
    recovered = [o for o in outcomes if o.recovered]
    return RunResult(
        mode=mode,
        split=split,
        seed=batch.seed,
        count=len(outcomes),
        at_risk=sum((o.amount for o in outcomes), Decimal("0")),
        recovered_count=len(recovered),
        recovered_amount=sum((o.amount for o in recovered), Decimal("0")),
        by_class=build_breakdown(rows),
        intervention_cost=total_cost,
        actions_taken=actions,
        blocked_by_policy=blocked,
        stopped_reasons=stopped,
        escalated_count=escalated_count,
        escalated_value=escalated_value,
        contacts_sent=sum(contacts.values()),
        max_contacts_per_customer=max(contacts.values(), default=0),
        counterfactual_recovered=sum(1 for o in recovered if o.counterfactual),
        violations=violations,
    )

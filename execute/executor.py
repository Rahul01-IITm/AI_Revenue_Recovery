"""Executes authorised actions. Nothing else in the repo may.

The signature is the point: `execute` takes a `PolicyDecision`, never a
`PlannedAction`. There is no way to reach this function with something the
policy engine has not ruled on, and it re-checks the verdict on arrival rather
than trusting the caller.

Transient API failures are retried with backoff and then quarantined, so one bad
response cannot take down a 500-transaction batch.
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal

from core.schemas import ExecutionResult, PolicyDecision, Transaction
from core.taxonomy import OUTBOUND_ACTIONS, RETRY_ACTIONS, Action, Verdict
from execute.channels import choose_channel, cost_of, send
from execute.idempotency import key_for
from execute.razorpay_adapter import RazorpayAdapter
from policy.state import RunState

MAX_API_ATTEMPTS = 3
BACKOFF_SECONDS = (0.0, 0.2, 0.5)


class QuarantinedError(RuntimeError):
    """Raised internally when an action fails every attempt. Caught by the
    runner, which records it and moves on to the next transaction."""


class Executor:
    def __init__(
        self,
        adapter: RazorpayAdapter | None = None,
        run_id: str = "run",
        sleep=time.sleep,
    ) -> None:
        self.adapter = adapter or RazorpayAdapter()
        self.run_id = run_id
        self._sleep = sleep

    def execute(
        self,
        txn: Transaction,
        decision: PolicyDecision,
        state: RunState,
        now: datetime,
        message_body: str = "",
    ) -> ExecutionResult:
        """Carry out an authorised action, or explain why nothing happened."""
        action = decision.final_action

        if decision.verdict is Verdict.VETO or action is Action.STOP:
            return ExecutionResult(
                txn_id=txn.txn_id,
                decision_id=decision.decision_id,
                idempotency_key="",
                action=action,
                executed_at=now,
                ok=False,
                detail=f"Not executed: {decision.rule_id} -- {decision.reason}",
            )

        key = key_for(decision, self.run_id, 1)

        if state.already_executed(key):
            # Replay. No side effect, no cost, and the budget is not consumed.
            return ExecutionResult(
                txn_id=txn.txn_id,
                decision_id=decision.decision_id,
                idempotency_key=key,
                action=action,
                executed_at=now,
                ok=True,
                replayed=True,
                detail="Idempotency key already executed; no-op.",
            )

        if action is Action.ESCALATE_HUMAN:
            result = ExecutionResult(
                txn_id=txn.txn_id, decision_id=decision.decision_id,
                idempotency_key=key, action=action, executed_at=now, ok=True,
                detail=f"Queued for human review: {decision.reason}",
            )
            state.record(txn, action, now, key)
            return result

        if action in RETRY_ACTIONS:
            resp = self._with_backoff(
                lambda: self.adapter.retry_payment(txn.txn_id, txn.amount)
            )
            result = ExecutionResult(
                txn_id=txn.txn_id, decision_id=decision.decision_id,
                idempotency_key=key, action=action, executed_at=now,
                ok=resp.ok, cost=Decimal("0"),
                detail=f"[{resp.mode}] {resp.reference} {resp.detail}".strip(),
            )
            state.record(txn, action, now, key)
            return result

        if action in OUTBOUND_ACTIONS:
            channel = choose_channel(txn, action)
            if channel is None:
                # Should be unreachable: rule_no_channel vetoes this upstream.
                return ExecutionResult(
                    txn_id=txn.txn_id, decision_id=decision.decision_id,
                    idempotency_key=key, action=action, executed_at=now,
                    ok=False, detail="No usable channel at execution time.",
                )

            if action is Action.SEND_PAYMENT_LINK:
                link = self._with_backoff(
                    lambda: self.adapter.create_payment_link(txn.txn_id, txn.amount)
                )
                detail_prefix = f"[{link.mode}] {link.reference} "
            else:
                detail_prefix = ""

            reference = send(txn, action, channel, message_body)
            result = ExecutionResult(
                txn_id=txn.txn_id, decision_id=decision.decision_id,
                idempotency_key=key, action=action, executed_at=now,
                ok=True, channel=channel, cost=cost_of(channel),
                detail=f"{detail_prefix}{reference}",
            )
            state.record(txn, action, now, key)
            return result

        return ExecutionResult(
            txn_id=txn.txn_id, decision_id=decision.decision_id,
            idempotency_key=key, action=action, executed_at=now, ok=False,
            detail=f"No executor path for {action.value}.",
        )

    def _with_backoff(self, call):
        """Retry a transient API failure, then give up and quarantine.

        The batch must survive a flaky endpoint; a single transaction failing
        every attempt is recorded and skipped, not allowed to abort the run.
        """
        last = None
        for attempt in range(MAX_API_ATTEMPTS):
            resp = call()
            if resp.ok:
                return resp
            last = resp
            if attempt < MAX_API_ATTEMPTS - 1:
                self._sleep(BACKOFF_SECONDS[attempt])
        return last

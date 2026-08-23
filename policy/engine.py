"""The gate between the AI and the money.

The planner proposes; this engine disposes. Nothing else in the repo may
authorise an action — the executor takes a `PolicyDecision`, never a
`PlannedAction`, so there is no path from planning to money that bypasses this
file.

The engine is deliberately dull: it runs a fixed list of pure rule functions in
a fixed order and records what happened. No LLM is consulted here, and none ever
should be.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from core.schemas import PlannedAction, PolicyDecision, Transaction
from core.taxonomy import Action, FailureClass, Verdict
from policy.rules import ORDERED_RULES, Ruling
from policy.state import RunState

#: Verdict severity, used to keep the most serious ruling when several fire.
_SEVERITY = {
    Verdict.ALLOW: 0,
    Verdict.DEFER: 1,
    Verdict.DOWNGRADE: 2,
    Verdict.VETO: 3,
}


class PolicyEngine:
    """Evaluates one proposed action against every guardrail."""

    def __init__(self, rules=ORDERED_RULES) -> None:
        self._rules = tuple(rules)

    def evaluate(
        self,
        txn: Transaction,
        planned: PlannedAction,
        state: RunState,
        now: datetime,
        failure_class: FailureClass | None = None,
    ) -> PolicyDecision:
        """Return the authorised action, or STOP if none is permitted.

        A VETO short-circuits: once an action is blocked outright there is
        nothing left for a timing rule to adjust, and reporting "vetoed, and
        also deferred" would be incoherent in the audit trail.
        """
        action = planned.action
        scheduled_at = planned.scheduled_at
        verdict = Verdict.ALLOW
        rule_id = "allow:default"
        reason = "No guardrail objected."

        for rule in self._rules:
            # Later rules must judge the action as amended so far, not the
            # original proposal — otherwise a DOWNGRADE to ESCALATE_HUMAN could
            # still be caught by a cap that only applies to what it replaced.
            current = planned.model_copy(
                update={"action": action, "scheduled_at": scheduled_at}
            )
            ruling: Ruling | None = rule(txn, current, state, now, failure_class)
            if ruling is None:
                continue

            if _SEVERITY[ruling.verdict] >= _SEVERITY[verdict]:
                verdict = ruling.verdict
                rule_id = ruling.rule_id
                reason = ruling.reason

            if ruling.replacement_action is not None:
                action = ruling.replacement_action
            if ruling.replacement_time is not None:
                scheduled_at = ruling.replacement_time

            if ruling.verdict is Verdict.VETO:
                break

        return PolicyDecision(
            decision_id=str(uuid.uuid4()),
            txn_id=txn.txn_id,
            decided_at=now,
            proposed_action=planned.action,
            proposed_at=planned.scheduled_at,
            verdict=verdict,
            final_action=action,
            final_scheduled_at=scheduled_at,
            rule_id=rule_id,
            reason=reason,
        )

    def authorise_or_stop(
        self,
        txn: Transaction,
        planned: PlannedAction,
        state: RunState,
        now: datetime,
        failure_class: FailureClass | None = None,
    ) -> tuple[PolicyDecision, bool]:
        """Convenience wrapper: `(decision, may_execute)`.

        `may_execute` is False for STOP and for anything vetoed, so callers
        cannot mistake a blocked decision for an authorised one by forgetting to
        check the verdict.
        """
        decision = self.evaluate(txn, planned, state, now, failure_class)
        may_execute = (
            decision.verdict is not Verdict.VETO
            and decision.final_action is not Action.STOP
        )
        return decision, may_execute

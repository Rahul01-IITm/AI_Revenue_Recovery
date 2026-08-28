"""Mutable counters the policy engine consults.

Kept separate from the rules so the rules stay pure functions of
`(txn, action, state)` — which is what makes them readable on screen and
trivial to test one at a time.

Counts are seeded from the transaction's own history (`retry_count`) and then
updated as the run executes, so a cap can be reached mid-batch rather than only
across batches.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from core.schemas import Transaction
from core.taxonomy import Action, OUTBOUND_ACTIONS, RETRY_ACTIONS


class RunState:
    """Per-run tally of what has already been done to whom."""

    def __init__(self) -> None:
        self._retries: dict[str, int] = defaultdict(int)
        self._messages_txn: dict[str, int] = defaultdict(int)
        self._messages_customer: dict[str, list[datetime]] = defaultdict(list)
        self._executed_keys: set[str] = set()
        self._escalations = 0

    # --- Reads ---------------------------------------------------------------

    def retries_used(self, txn: Transaction) -> int:
        """Prior retries plus retries executed during this run."""
        return txn.retry_count + self._retries[txn.txn_id]

    def messages_for_txn(self, txn_id: str) -> int:
        return self._messages_txn[txn_id]

    def messages_for_customer_7d(self, customer_id: str, now: datetime) -> int:
        cutoff = now - timedelta(days=7)
        return sum(1 for t in self._messages_customer[customer_id] if t >= cutoff)

    def already_executed(self, idempotency_key: str) -> bool:
        return idempotency_key in self._executed_keys

    def escalations_used(self) -> int:
        return self._escalations

    # --- Writes --------------------------------------------------------------

    def record(
        self, txn: Transaction, action: Action, at: datetime, key: str | None = None
    ) -> None:
        """Record an executed action so later rules see it.

        Called only by the executor, and only for actions that actually took
        effect — a replayed idempotent action must not consume budget.
        """
        if action in RETRY_ACTIONS:
            self._retries[txn.txn_id] += 1
        if action in OUTBOUND_ACTIONS:
            self._messages_txn[txn.txn_id] += 1
            self._messages_customer[txn.customer_id].append(at)
        if action is Action.ESCALATE_HUMAN:
            self._escalations += 1
        if key:
            self._executed_keys.add(key)

    def seed_customer_history(self, customer_id: str, sent_at: list[datetime]) -> None:
        """Pre-load prior contacts, e.g. from the audit store on a resumed run."""
        self._messages_customer[customer_id].extend(sent_at)

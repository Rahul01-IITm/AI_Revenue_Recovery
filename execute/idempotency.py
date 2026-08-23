"""Idempotency keys. A replayed run must not charge anyone twice.

The key is derived from what makes an action unique — transaction, action, and
ladder rung — and deliberately *not* from the wall clock or a UUID, so that
re-running the same batch produces the same keys and the replay is detected.

This is one of the demo drills: run the batch twice, show the second run
executing nothing and costing nothing.
"""

from __future__ import annotations

import hashlib

from core.schemas import PolicyDecision


def idempotency_key(
    run_id: str, txn_id: str, action: str, attempt_no: int
) -> str:
    """Stable key for one (run, transaction, action, rung).

    `run_id` is included so a *deliberate* re-run under a new id can proceed,
    while an accidental replay of the same run cannot. Callers wanting strict
    cross-run deduplication pass a constant run_id.
    """
    raw = f"{run_id}|{txn_id}|{action}|{attempt_no}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def key_for(decision: PolicyDecision, run_id: str, attempt_no: int) -> str:
    return idempotency_key(
        run_id, decision.txn_id, decision.final_action.value, attempt_no
    )

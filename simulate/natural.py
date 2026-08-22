"""Column A of the simulator: natural recovery with no intervention.

The constants here are a spec, not a tuning knob. They are transcribed from
`SIMULATION_ASSUMPTIONS.md`, which is frozen. `tests/test_natural.py` asserts
the two files agree, so editing one without the other fails the suite.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from core.schemas import Transaction
from core.taxonomy import FailureClass

#: P(recovery | failure_class, action = NONE) within the 72h window.
#: Frozen 2026-08-22. Changing a value here invalidates every published baseline.
P_NATURAL_RECOVERY: dict[FailureClass, float] = {
    FailureClass.ISSUER_DOWN: 0.30,
    FailureClass.THREE_DS_TIMEOUT: 0.18,
    FailureClass.INSUFFICIENT_FUNDS: 0.12,
    FailureClass.CHECKOUT_ABANDONED: 0.10,
    FailureClass.CARD_EXPIRED: 0.05,
    FailureClass.DO_NOT_HONOUR: 0.05,
    FailureClass.MANDATE_EXPIRED: 0.04,
    FailureClass.MANDATE_REVOKED: 0.01,
    FailureClass.SUSPECTED_FRAUD: 0.00,
    FailureClass.INVALID_ACCOUNT: 0.00,
    FailureClass.B2B_INVOICE_OVERDUE: 0.00,
}


def _uniform(seed: int, txn_id: str, stream: str) -> float:
    """A stable uniform draw in [0, 1) keyed by transaction, not by call order.

    Uses BLAKE2b rather than `hash()`, which is salted per process and would make
    runs irreproducible across interpreter restarts.
    """
    digest = hashlib.blake2b(
        f"{seed}|{txn_id}|{stream}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") / 2**64


def recovers_naturally(
    txn: Transaction, true_class: FailureClass, *, seed: int
) -> bool:
    """Would this transaction have recovered on its own, with no intervention?

    Paired across modes: the draw depends only on `(seed, txn_id)`, so the
    do-nothing, naive-retry, and agent runs all see the same luck for the same
    transaction. See "Draw mechanics" in SIMULATION_ASSUMPTIONS.md.
    """
    return _uniform(seed, txn.txn_id, "natural") < P_NATURAL_RECOVERY[true_class]


def expected_natural_recovery(
    txns: list[Transaction], true_classes: dict[str, FailureClass]
) -> Decimal:
    """Analytic expectation of recovered value, independent of any draw.

    Used in tests to confirm the sampled baseline sits near its expectation, so a
    broken RNG surfaces as a test failure rather than as a plausible-looking
    number in the deck.
    """
    total = Decimal("0")
    for t in txns:
        p = Decimal(str(P_NATURAL_RECOVERY[true_classes[t.txn_id]]))
        total += t.amount * p
    return total

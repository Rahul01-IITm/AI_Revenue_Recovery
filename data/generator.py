"""Seeded synthetic batch generator for the D2C subscriptions vertical.

Three properties this file exists to guarantee:

1. **Reproducible.** Same seed, same batch, byte for byte. No `datetime.now()`,
   no unseeded `random`, no set iteration feeding an ordered decision.
2. **Coherent.** A `MANDATE_EXPIRED` row is on an e-mandate with
   `mandate_status=EXPIRED` and `is_subscription=True`. Incoherent rows are the
   fastest way for a judge to lose trust in the batch.
3. **Awkward on purpose.** The planted cases in `_plant_edge_cases` are what the
   live failure drills demo against. They are not decoration.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.schemas import Batch, GroundTruth, Transaction
from core.taxonomy import Channel, FailureClass, MandateStatus, PaymentMethod

DEFAULT_SEED = 42

#: Fixed clock. Using the wall clock here would make batches irreproducible.
REFERENCE_TIME = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)

#: Failure mix for D2C subscriptions (decided 21 Aug). Deliberately non-uniform:
#: insufficient funds and issuer downtime dominate, fraud is rare. A flat
#: distribution reads as synthetic on sight.
#:
#: The spec's single 11% MANDATE_EXPIRED bucket is split 8/3 with MANDATE_REVOKED
#: so both mandate paths are exercised — they share an action but have very
#: different natural-recovery rates (0.04 vs 0.01).
FAILURE_MIX: dict[FailureClass, float] = {
    FailureClass.INSUFFICIENT_FUNDS: 0.32,
    FailureClass.ISSUER_DOWN: 0.18,
    FailureClass.CARD_EXPIRED: 0.12,
    FailureClass.THREE_DS_TIMEOUT: 0.10,
    FailureClass.MANDATE_EXPIRED: 0.08,
    FailureClass.CHECKOUT_ABANDONED: 0.08,
    FailureClass.DO_NOT_HONOUR: 0.05,
    FailureClass.MANDATE_REVOKED: 0.03,
    FailureClass.SUSPECTED_FRAUD: 0.02,
    FailureClass.INVALID_ACCOUNT: 0.02,
}

#: Indian D2C subscription prices cluster at psychological price points rather
#: than spreading uniformly. Weighted toward the low end.
MONTHLY_PRICE_POINTS: list[tuple[Decimal, float]] = [
    (Decimal("149.00"), 0.10),
    (Decimal("199.00"), 0.14),
    (Decimal("249.00"), 0.08),
    (Decimal("299.00"), 0.12),
    (Decimal("399.00"), 0.07),
    (Decimal("499.00"), 0.12),
    (Decimal("599.00"), 0.06),
    (Decimal("799.00"), 0.05),
    (Decimal("999.00"), 0.09),
    (Decimal("1299.00"), 0.04),
    (Decimal("1499.00"), 0.04),
    (Decimal("1999.00"), 0.03),
    (Decimal("2499.00"), 0.02),
    (Decimal("2999.00"), 0.02),
    (Decimal("4999.00"), 0.02),
]

#: Annual plans. Real D2C services sell these at a discount to the monthly rate,
#: and they matter here for a measurement reason as much as a realism one:
#: without a mid-value tier the amount distribution is barbelled, the planted
#: Rs.50k+ rows dominate total at-risk value, and the value-weighted headline
#: swings wildly on a handful of draws.
ANNUAL_PRICE_POINTS: list[tuple[Decimal, float]] = [
    (Decimal("5999.00"), 0.34),
    (Decimal("7999.00"), 0.28),
    (Decimal("9999.00"), 0.26),
    (Decimal("11999.00"), 0.12),
]

#: Share of transactions on an annual plan rather than a monthly one.
ANNUAL_SHARE = 0.10

#: Observable gateway signal per class. Note `GW_05` is shared by DO_NOT_HONOUR
#: and INSUFFICIENT_FUNDS — real gateways are ambiguous, and the diagnosis layer
#: has to earn its accuracy rather than reading a clean label.
FAILURE_SIGNALS: dict[FailureClass, list[tuple[str, str]]] = {
    FailureClass.INSUFFICIENT_FUNDS: [
        ("GW_51", "insufficient funds in account"),
        ("GW_51", "Transaction declined - low balance"),
        ("GW_05", "do not honour - insufficient balance per issuer"),
    ],
    FailureClass.ISSUER_DOWN: [
        ("GW_91", "issuer unavailable, please retry"),
        ("GW_91", "Gateway timeout contacting issuing bank"),
        ("GW_TO", "upstream timeout after 30000ms"),
    ],
    FailureClass.CARD_EXPIRED: [
        ("GW_54", "expired card"),
        ("GW_54", "Card expiry date is in the past"),
    ],
    FailureClass.THREE_DS_TIMEOUT: [
        ("GW_3DS", "3DS authentication timed out"),
        ("GW_3DS", "customer did not complete OTP within 180s"),
    ],
    FailureClass.MANDATE_EXPIRED: [
        ("GW_MND", "mandate expired"),
        ("GW_MND", "e-mandate validity period has elapsed"),
    ],
    FailureClass.MANDATE_REVOKED: [
        ("GW_MND", "mandate revoked by customer"),
        ("GW_MND", "debit authorisation cancelled at bank"),
    ],
    FailureClass.CHECKOUT_ABANDONED: [
        ("GW_ABD", "session abandoned before payment"),
        ("GW_ABD", "customer closed checkout"),
    ],
    FailureClass.DO_NOT_HONOUR: [
        ("GW_05", "do not honour"),
        ("GW_05", "Declined by issuer without reason code"),
    ],
    FailureClass.SUSPECTED_FRAUD: [
        ("GW_FRD", "suspected fraudulent transaction"),
        ("GW_FRD", "card reported stolen"),
    ],
    FailureClass.INVALID_ACCOUNT: [
        ("GW_14", "invalid account number"),
        ("GW_14", "no such account at issuing bank"),
    ],
}

#: Genuinely undecidable signals: the gateway text is indistinguishable from a
#: *different* class's text, so no rule can get these right.
#:
#: Without these the batch is perfectly separable by the same rules we wrote to
#: classify it, diagnosis scores 100%, and the confidence bands mean nothing.
#: Real issuers return a bare `05` for a balance decline and a bare mandate
#: error for both expiry and revocation; that irreducible noise belongs in the
#: data. It is also precisely the population the LLM layer (step 7) must beat.
AMBIGUOUS_SIGNALS: dict[FailureClass, list[tuple[str, str]]] = {
    FailureClass.INSUFFICIENT_FUNDS: [
        ("GW_05", "do not honour"),
        ("GW_05", "Declined by issuer without reason code"),
    ],
    FailureClass.DO_NOT_HONOUR: [
        ("GW_05", "declined - insufficient balance reported"),
    ],
    FailureClass.MANDATE_EXPIRED: [
        ("GW_MND", "debit authorisation cancelled at bank"),
    ],
    FailureClass.MANDATE_REVOKED: [
        ("GW_MND", "mandate no longer active"),
    ],
}

#: Codes no rule recognises. These must come out UNCLASSIFIED rather than
#: guessed, and they are the step 7 LLM layer's target population.
UNKNOWN_SIGNALS: list[tuple[str, str]] = [
    ("GW_UNK", "unspecified processing error"),
    ("GW_999", "refer to issuer"),
    ("", "error"),
]

#: Share of rows carrying an undecidable signal, and a wholly unknown one.
AMBIGUOUS_SIGNAL_RATE = 0.06
UNKNOWN_SIGNAL_RATE = 0.02

#: Payment method implied by the failure class, where physics demands it.
#: `None` means any method is plausible and one is drawn.
METHOD_FOR_CLASS: dict[FailureClass, PaymentMethod | None] = {
    FailureClass.CARD_EXPIRED: PaymentMethod.CARD,
    FailureClass.THREE_DS_TIMEOUT: PaymentMethod.CARD,
    FailureClass.SUSPECTED_FRAUD: PaymentMethod.CARD,
    FailureClass.MANDATE_EXPIRED: PaymentMethod.EMANDATE,
    FailureClass.MANDATE_REVOKED: PaymentMethod.EMANDATE,
    FailureClass.INVALID_ACCOUNT: PaymentMethod.NETBANKING,
    FailureClass.INSUFFICIENT_FUNDS: None,
    FailureClass.ISSUER_DOWN: None,
    FailureClass.CHECKOUT_ABANDONED: None,
    FailureClass.DO_NOT_HONOUR: None,
}

GENERIC_METHODS = [
    PaymentMethod.CARD,
    PaymentMethod.UPI,
    PaymentMethod.NETBANKING,
    PaymentMethod.WALLET,
    PaymentMethod.EMANDATE,
]

MERCHANTS = ["mrc_streamly", "mrc_fitpass", "mrc_boxfresh", "mrc_learnhub"]

#: Fraction of the batch held out for reporting. Numbers in the deck come from
#: `test` only; tuning happens on `train`.
TEST_FRACTION = 0.40


def _weighted(rng: random.Random, weights: dict) -> object:
    """Draw one key by weight. Sorted for determinism across dict orderings."""
    items = sorted(weights.items(), key=lambda kv: str(kv[0]))
    keys = [k for k, _ in items]
    vals = [v for _, v in items]
    return rng.choices(keys, weights=vals, k=1)[0]


def _draw_amount(rng: random.Random) -> Decimal:
    """Draw a subscription price. Annual plans are the mid-value tier."""
    points = (
        ANNUAL_PRICE_POINTS
        if rng.random() < ANNUAL_SHARE
        else MONTHLY_PRICE_POINTS
    )
    amounts = [a for a, _ in points]
    weights = [w for _, w in points]
    return rng.choices(amounts, weights=weights, k=1)[0]


def _draw_signal(rng: random.Random, cls: FailureClass) -> tuple[str, str]:
    """Draw the observable `(failure_code, failure_message)` for a class.

    Mostly clean signals, with a deliberate minority that are undecidable or
    unrecognisable. The classifier is expected to lose points on these — that is
    the point. A diagnosis layer that scores 100% is being graded on data built
    to suit it.
    """
    roll = rng.random()
    if roll < UNKNOWN_SIGNAL_RATE:
        return rng.choice(UNKNOWN_SIGNALS)
    if roll < UNKNOWN_SIGNAL_RATE + AMBIGUOUS_SIGNAL_RATE:
        confusable = AMBIGUOUS_SIGNALS.get(cls)
        if confusable:
            return rng.choice(confusable)
    return rng.choice(FAILURE_SIGNALS[cls])


def _mandate_status_for(
    rng: random.Random, cls: FailureClass, method: PaymentMethod, is_sub: bool
) -> MandateStatus:
    if cls is FailureClass.MANDATE_EXPIRED:
        return MandateStatus.EXPIRED
    if cls is FailureClass.MANDATE_REVOKED:
        return MandateStatus.REVOKED
    if method is PaymentMethod.EMANDATE and is_sub:
        return MandateStatus.ACTIVE
    return MandateStatus.NONE


def _make_customer_pool(rng: random.Random, n_customers: int) -> list[dict]:
    """Customers exist independently of transactions so that one customer can own
    several failures — which is what makes MAX_MESSAGES_PER_CUSTOMER_7D testable.
    """
    pool = []
    for i in range(n_customers):
        pool.append(
            {
                "customer_id": f"cust_{i:05d}",
                # ~60% have a known payday; the rest force the planner's
                # documented T+72h fallback.
                "salary_day": rng.choice([1, 1, 5, 7, 25, 28, 30]) if rng.random() < 0.60 else None,
                "customer_ltv": Decimal(str(rng.choice([0, 499, 1200, 3600, 8400, 24000]))),
                "prior_success_rate": round(rng.betavariate(8, 2), 3),
                "opted_out": rng.random() < 0.04,
                "channel_prefs": _draw_channels(rng),
            }
        )
    return pool


def _draw_channels(rng: random.Random) -> tuple[Channel, ...]:
    roll = rng.random()
    if roll < 0.55:
        return (Channel.WHATSAPP, Channel.EMAIL)
    if roll < 0.80:
        return (Channel.EMAIL,)
    if roll < 0.95:
        return (Channel.WHATSAPP, Channel.SMS, Channel.EMAIL)
    return ()  # unreachable by any channel; outbound must be suppressed


def generate_batch(n: int = 500, seed: int = DEFAULT_SEED) -> Batch:
    """Generate a reproducible batch of `n` at-risk D2C subscription payments."""
    rng = random.Random(seed)

    # Fewer customers than transactions, so repeat offenders arise naturally.
    pool = _make_customer_pool(rng, n_customers=int(n * 0.80))

    txns: list[Transaction] = []
    truth: dict[str, GroundTruth] = {}

    for i in range(n):
        cls = _weighted(rng, FAILURE_MIX)
        cust = pool[rng.randrange(len(pool))]

        method = METHOD_FOR_CLASS[cls] or rng.choice(GENERIC_METHODS)
        is_sub = method is PaymentMethod.EMANDATE or rng.random() < 0.70
        code, message = _draw_signal(rng, cls)

        # Spread over the last 7 days across all hours, so some failures land
        # inside quiet hours (21:00-09:00 IST) and exercise that guardrail.
        ts = REFERENCE_TIME - timedelta(
            hours=rng.randrange(0, 168), minutes=rng.randrange(0, 60)
        )

        txn = Transaction(
            txn_id=f"txn_{i:06d}",
            customer_id=cust["customer_id"],
            merchant_id=rng.choice(MERCHANTS),
            amount=_draw_amount(rng),
            timestamp=ts,
            method=method,
            failure_code=code,
            failure_message=message,
            retry_count=rng.choices([0, 1, 2], weights=[0.75, 0.20, 0.05], k=1)[0],
            customer_ltv=cust["customer_ltv"],
            prior_success_rate=cust["prior_success_rate"],
            prior_failures_7d=rng.choices([0, 1, 2], weights=[0.70, 0.22, 0.08], k=1)[0],
            is_subscription=is_sub,
            mandate_status=_mandate_status_for(rng, cls, method, is_sub),
            opted_out=cust["opted_out"],
            channel_prefs=cust["channel_prefs"],
            salary_day=cust["salary_day"],
        )
        txns.append(txn)
        truth[txn.txn_id] = GroundTruth(
            txn_id=txn.txn_id,
            true_failure_class=cls,
            split="train",  # assigned properly below
        )

    txns, truth = _plant_edge_cases(rng, txns, truth)
    truth = _assign_splits(txns, truth, seed)

    return Batch(
        seed=seed,
        generated_at=REFERENCE_TIME,
        transactions=txns,
        ground_truth=truth,
    )


def _plant_edge_cases(
    rng: random.Random,
    txns: list[Transaction],
    truth: dict[str, GroundTruth],
) -> tuple[list[Transaction], dict[str, GroundTruth]]:
    """Overwrite specific rows with the awkward cases the demo drills against.

    Each planted case maps to a guardrail that must visibly fire:

    - high value        -> HUMAN_APPROVAL_THRESHOLD (ESCALATE_HUMAN)
    - retry exhausted   -> MAX_RETRIES_PER_TXN (STOP)
    - chargeback open   -> NEVER_RETRY
    - opted out + fraud -> naive-retry-all contacts/retries it, we do not
    - repeat offender   -> MAX_MESSAGES_PER_CUSTOMER_7D
    """
    by_id = {t.txn_id: i for i, t in enumerate(txns)}

    def replace(txn_id: str, cls: FailureClass | None = None, **changes) -> None:
        """Overwrite one row, keeping it internally coherent.

        Skips silently when the id is absent: the planted ids assume a batch of
        at least ~300 rows, and the small batches used in unit tests do not need
        edge cases. `tests/test_generator.py` asserts every case is present at
        n=500, which is the size that actually ships.
        """
        idx = by_id.get(txn_id)
        if idx is None:
            return

        if cls is not None:
            # The observable signal must match the new class, or the row is
            # self-contradictory and the classifier is measured against data
            # that cannot be got right. Draw a signal unless one was supplied.
            if "failure_code" not in changes:
                code, msg = rng.choice(FAILURE_SIGNALS[cls])
                changes["failure_code"] = code
                changes.setdefault("failure_message", msg)

            # Changing the class can invalidate method and mandate_status. Derive
            # them rather than trusting each call site to remember.
            implied = METHOD_FOR_CLASS[cls]
            if implied is not None:
                changes.setdefault("method", implied)
            method = changes.get("method", txns[idx].method)
            is_sub = changes.get("is_subscription", txns[idx].is_subscription)
            if method is PaymentMethod.EMANDATE:
                changes.setdefault("is_subscription", True)
                is_sub = True
            changes.setdefault(
                "mandate_status", _mandate_status_for(rng, cls, method, is_sub)
            )
            truth[txn_id] = truth[txn_id].model_copy(update={"true_failure_class": cls})

        txns[idx] = txns[idx].model_copy(update=changes)

    # 1. Three transactions over the Rs.50,000 approval threshold. Plural, so the
    #    gate is not demonstrated on a single cherry-picked row.
    replace("txn_000007", FailureClass.ISSUER_DOWN,
            amount=Decimal("120000.00"), failure_code="GW_91",
            failure_message="issuer unavailable, please retry")
    replace("txn_000031", FailureClass.INSUFFICIENT_FUNDS,
            amount=Decimal("74999.00"), failure_code="GW_51",
            failure_message="insufficient funds in account")
    replace("txn_000112", FailureClass.CARD_EXPIRED,
            amount=Decimal("55000.00"), failure_code="GW_54",
            failure_message="expired card")

    # 2. Retry budget already exhausted -> policy must STOP, not retry a third time.
    replace("txn_000019", FailureClass.ISSUER_DOWN, retry_count=2)
    replace("txn_000203", FailureClass.INSUFFICIENT_FUNDS, retry_count=2)

    # 3. Chargeback already open -> hard block regardless of failure class.
    replace("txn_000044", chargeback_open=True)
    replace("txn_000288", chargeback_open=True)

    # 4. Opted-out customer on a recoverable failure. Naive-retry-all will message
    #    them; we must suppress every outbound action. This contrast is the demo.
    replace("txn_000061", FailureClass.CHECKOUT_ABANDONED, opted_out=True)
    replace("txn_000155", FailureClass.THREE_DS_TIMEOUT, opted_out=True)

    # 5. A customer with four failures in seven days -> cross-transaction message
    #    cap. All four share one customer_id so the cap is reachable.
    for j, tid in enumerate(
        ["txn_000090", "txn_000091", "txn_000092", "txn_000093"]
    ):
        replace(tid, FailureClass.INSUFFICIENT_FUNDS,
                customer_id="cust_repeat_01", prior_failures_7d=4,
                opted_out=False, channel_prefs=(Channel.WHATSAPP, Channel.EMAIL),
                salary_day=1, retry_count=0,
                timestamp=REFERENCE_TIME - timedelta(days=6 - j, hours=3))

    # 6. Fraud on a high-value row. Naive-retry-all retries this; that is a
    #    compliance incident and the single sharpest contrast in the deck.
    replace("txn_000005", FailureClass.SUSPECTED_FRAUD,
            amount=Decimal("18999.00"), failure_code="GW_FRD",
            failure_message="card reported stolen")

    return txns, truth


def _assign_splits(
    txns: list[Transaction], truth: dict[str, GroundTruth], seed: int
) -> dict[str, GroundTruth]:
    """Deterministic train/test split, stable under reordering or resizing.

    Hash-based rather than index-based so that generating 1000 transactions does
    not reshuffle which of the first 500 were test rows.
    """
    import hashlib

    out: dict[str, GroundTruth] = {}
    for t in txns:
        h = hashlib.blake2b(f"{seed}|{t.txn_id}|split".encode(), digest_size=8).digest()
        u = int.from_bytes(h, "big") / 2**64
        split = "test" if u < TEST_FRACTION else "train"
        out[t.txn_id] = truth[t.txn_id].model_copy(update={"split": split})
    return out

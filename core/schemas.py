"""Canonical data contracts.

`Transaction` is what the *agent* is allowed to see. `GroundTruth` is what the
*world* knows. They are separate models on purpose: if the true failure class
lived on `Transaction`, the diagnosis layer could read it and every accuracy
number afterwards would be meaningless. The simulator is entitled to ground
truth because it plays the role of reality; nothing upstream of it is.

Money is `Decimal`, never `float`. Summing 500 float rupee amounts and reporting
the total to the nearest paise is a bug waiting for a judge to find it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.taxonomy import (
    Channel,
    FailureClass,
    MandateStatus,
    PaymentMethod,
    Recoverability,
)

Rupees = Annotated[Decimal, Field(ge=0, decimal_places=2)]

Split = Literal["train", "test"]


class Transaction(BaseModel):
    """One at-risk payment, as the agent sees it.

    Deliberately excludes the true failure class. `failure_code` and
    `failure_message` are the observable signals the diagnosis layer works from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    txn_id: str
    customer_id: str
    merchant_id: str

    amount: Rupees
    currency: Literal["INR"] = "INR"
    timestamp: datetime
    """When the payment failed. Recovery windows are measured from here."""

    method: PaymentMethod

    failure_code: str
    """Raw gateway code, e.g. `GW_05`. Observable; ambiguous by design."""
    failure_message: str
    """Unstructured gateway text. The LLM layer (step 7) parses this; the rules
    path keys off `failure_code` alone so it works with `--no-llm`."""

    retry_count: int = Field(ge=0)
    """Retries already attempted before this batch. Counts against MAX_RETRIES_PER_TXN."""

    customer_ltv: Rupees
    prior_success_rate: float = Field(ge=0.0, le=1.0)
    prior_failures_7d: int = Field(ge=0)

    is_subscription: bool
    mandate_status: MandateStatus
    opted_out: bool
    """Absolute. Checked before every outbound action, no exceptions."""

    channel_prefs: tuple[Channel, ...] = ()

    # --- Fields added beyond the CLAUDE.md list, with justification ---

    salary_day: int | None = Field(default=None, ge=1, le=31)
    """Day-of-month the customer is typically paid. Required to compute the
    RETRY_SALARY_ALIGNED window; `None` means unknown and the planner must fall
    back to the spec's T+72h. Roughly 40% of customers have no value."""

    chargeback_open: bool = False
    """CLAUDE.md lists CHARGEBACK_OPEN in NEVER_RETRY, but it is a transaction
    state rather than a failure class, so it cannot live in `FailureClass`.
    Without this flag that guardrail is unimplementable."""

    @field_validator("channel_prefs", mode="before")
    @classmethod
    def _coerce_channels(cls, v: object) -> object:
        return tuple(v) if isinstance(v, list) else v

    @property
    def is_contactable(self) -> bool:
        """Cheap precondition for any outbound action. Not a substitute for the
        policy engine, which owns quiet hours, caps, and the real verdict."""
        return not self.opted_out and bool(self.channel_prefs)


class GroundTruth(BaseModel):
    """What actually happened, known only to the generator and the simulator.

    Kept out of `Transaction` so that no code path can accidentally let the agent
    peek. Stored as a sidecar map keyed by `txn_id`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    txn_id: str
    true_failure_class: FailureClass
    split: Split


class Diagnosis(BaseModel):
    """What the agent *believes* about a failure, and why.

    `failure_class=None` means the rules could not classify the row. That is a
    legitimate, auditable outcome — the planner must not auto-act on an
    undiagnosed transaction. It is also exactly the population the LLM layer
    (step 7) is meant to improve, which is why it is modelled rather than
    papered over with a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    txn_id: str
    failure_class: FailureClass | None
    confidence: float = Field(ge=0.0, le=1.0)

    rule_id: str
    """Which rule fired. Goes straight into the audit trail."""
    rationale: str
    """Human-readable justification, reconstructable after the fact."""

    source: Literal["rules", "llm"] = "rules"
    """The `--no-llm` path only ever produces `rules`. Step 7 adds the other."""

    @property
    def is_classified(self) -> bool:
        return self.failure_class is not None


class RecoverabilityAssessment(BaseModel):
    """How winnable this transaction looks, given the diagnosis and the customer.

    A *signal*, not a decision. The policy engine can still veto a high score,
    and the planner can still decline to act on one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    txn_id: str
    score: float = Field(ge=0.0, le=1.0)
    tier: Recoverability
    confidence: float = Field(ge=0.0, le=1.0)
    """Carried through from the diagnosis, deliberately not blended into
    `score` — a confident low score and a guessed low score warrant different
    handling, and mixing them destroys that distinction."""

    factors: tuple[str, ...] = ()
    """Every adjustment that moved the score, for the audit trail."""


class Batch(BaseModel):
    """A generated batch: transactions, their ground truth, and the split.

    Serialised whole so a run is reproducible from the file alone, not just from
    the seed plus the generator version.
    """

    model_config = ConfigDict(extra="forbid")

    seed: int
    generated_at: datetime
    vertical: Literal["D2C_SUBSCRIPTIONS"] = "D2C_SUBSCRIPTIONS"

    transactions: list[Transaction]
    ground_truth: dict[str, GroundTruth]

    def split_of(self, txn_id: str) -> Split:
        return self.ground_truth[txn_id].split

    def true_class(self, txn_id: str) -> FailureClass:
        """Simulator-only. Calling this from diagnosis or planning is a bug."""
        return self.ground_truth[txn_id].true_failure_class

    def select(self, split: Split | None = None) -> list[Transaction]:
        """Transactions in a split. `None` returns everything.

        Reporting always passes `split="test"`. See `report.metrics`.
        """
        if split is None:
            return list(self.transactions)
        return [t for t in self.transactions if self.split_of(t.txn_id) == split]

    @property
    def total_at_risk(self) -> Decimal:
        return sum((t.amount for t in self.transactions), Decimal("0"))

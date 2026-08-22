"""Failure classes, allowed actions, and the map between them.

This module is the domain vocabulary. Every other stage types against it.

Two rules that outrank convenience:

1. `Action` is a closed set. The planner may emit nothing outside it. Adding a
   member means updating this module, the intervention map, and the tests in the
   same commit.
2. `FailureClass` is ground truth about *the world*. The diagnosis layer infers
   it from `failure_code` / `failure_message` and is never handed the answer.
   See `core.schemas.GroundTruth` for how that separation is enforced.
"""

from __future__ import annotations

from enum import StrEnum


class FailureClass(StrEnum):
    """Why a payment failed, normalised across gateways."""

    ISSUER_DOWN = "ISSUER_DOWN"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    CARD_EXPIRED = "CARD_EXPIRED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    THREE_DS_TIMEOUT = "3DS_TIMEOUT"
    CHECKOUT_ABANDONED = "CHECKOUT_ABANDONED"
    DO_NOT_HONOUR = "DO_NOT_HONOUR"
    SUSPECTED_FRAUD = "SUSPECTED_FRAUD"
    INVALID_ACCOUNT = "INVALID_ACCOUNT"
    B2B_INVOICE_OVERDUE = "B2B_INVOICE_OVERDUE"
    """Out of scope for the D2C vertical (decided 21 Aug). Kept so the taxonomy
    stays complete, but the generator emits none of these."""


class Action(StrEnum):
    """The closed set of interventions the planner may propose.

    The policy engine may VETO or DOWNGRADE any of these, but the planner can
    never invent one outside the set.
    """

    RETRY = "RETRY"
    RETRY_SALARY_ALIGNED = "RETRY_SALARY_ALIGNED"
    SEND_PAYMENT_LINK = "SEND_PAYMENT_LINK"
    NUDGE_WHATSAPP = "NUDGE_WHATSAPP"
    NUDGE_EMAIL = "NUDGE_EMAIL"
    REQUEST_INSTRUMENT_UPDATE = "REQUEST_INSTRUMENT_UPDATE"
    REQUEST_MANDATE_RENEWAL = "REQUEST_MANDATE_RENEWAL"
    OFFER_PARTIAL_PLAN = "OFFER_PARTIAL_PLAN"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    STOP = "STOP"


class Recoverability(StrEnum):
    """Prior on how winnable a failure class is, before per-transaction signals."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NEVER = "NEVER"


#: Failure classes that must never be retried, whatever the planner proposes.
#: `CHARGEBACK_OPEN` from the spec is a transaction *state*, not a failure class,
#: so it is enforced separately via `Transaction.chargeback_open`.
NEVER_RETRY: frozenset[FailureClass] = frozenset(
    {FailureClass.SUSPECTED_FRAUD, FailureClass.INVALID_ACCOUNT}
)

#: Recoverability prior per failure class. Mirrors the CLAUDE.md taxonomy table.
RECOVERABILITY: dict[FailureClass, Recoverability] = {
    FailureClass.ISSUER_DOWN: Recoverability.HIGH,
    FailureClass.INSUFFICIENT_FUNDS: Recoverability.MEDIUM,
    FailureClass.CARD_EXPIRED: Recoverability.MEDIUM,
    FailureClass.MANDATE_EXPIRED: Recoverability.MEDIUM,
    FailureClass.MANDATE_REVOKED: Recoverability.MEDIUM,
    FailureClass.THREE_DS_TIMEOUT: Recoverability.HIGH,
    FailureClass.CHECKOUT_ABANDONED: Recoverability.MEDIUM,
    FailureClass.DO_NOT_HONOUR: Recoverability.LOW,
    FailureClass.SUSPECTED_FRAUD: Recoverability.NEVER,
    FailureClass.INVALID_ACCOUNT: Recoverability.NEVER,
    FailureClass.B2B_INVOICE_OVERDUE: Recoverability.HIGH,
}

#: Intended intervention per failure class, as an ordered ladder.
#:
#: This is the *planner's* reference, not policy. The policy engine still gates
#: every entry here. Step 1 only needs this to assert that distinct failure
#: classes lead to visibly different behaviour; the planner lands in step 4.
INTERVENTION_LADDER: dict[FailureClass, tuple[Action, ...]] = {
    FailureClass.ISSUER_DOWN: (Action.RETRY, Action.RETRY),
    FailureClass.INSUFFICIENT_FUNDS: (Action.RETRY_SALARY_ALIGNED,),
    FailureClass.CARD_EXPIRED: (Action.REQUEST_INSTRUMENT_UPDATE,),
    FailureClass.MANDATE_EXPIRED: (Action.REQUEST_MANDATE_RENEWAL,),
    FailureClass.MANDATE_REVOKED: (Action.REQUEST_MANDATE_RENEWAL,),
    FailureClass.THREE_DS_TIMEOUT: (Action.SEND_PAYMENT_LINK,),
    FailureClass.CHECKOUT_ABANDONED: (Action.NUDGE_WHATSAPP, Action.SEND_PAYMENT_LINK),
    # CLAUDE.md's table says `SINGLE_RETRY`, which is not in the Action enum. The
    # enum is authoritative, so this is one RETRY and the ladder simply stops.
    FailureClass.DO_NOT_HONOUR: (Action.RETRY,),
    FailureClass.SUSPECTED_FRAUD: (Action.STOP,),
    FailureClass.INVALID_ACCOUNT: (Action.STOP,),
    FailureClass.B2B_INVOICE_OVERDUE: (
        Action.NUDGE_EMAIL,
        Action.NUDGE_EMAIL,
        Action.ESCALATE_HUMAN,
    ),
}


class PaymentMethod(StrEnum):
    CARD = "CARD"
    UPI = "UPI"
    NETBANKING = "NETBANKING"
    WALLET = "WALLET"
    EMANDATE = "EMANDATE"


class MandateStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    NONE = "NONE"
    """Not a subscription, or no mandate was ever registered."""


class Channel(StrEnum):
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    SMS = "SMS"

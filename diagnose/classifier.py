"""Rules-based failure classification. No ML, no LLM.

Reads `failure_code` and `failure_message` and nothing else. It never sees
`GroundTruth`, which is what makes the accuracy number in `report.diagnosis`
worth quoting.

Real gateways are ambiguous: `GW_05` is returned for both a generic
`DO_NOT_HONOUR` and for an issuer declining on balance, and `GW_MND` covers both
mandate expiry and mandate revocation. The code alone is therefore not enough,
and the keyword layer below exists to break exactly those ties.

This module is the `--no-llm` path and must keep working when the LLM layer
lands in step 7. Step 7 upgrades the `source="llm"` seam; it does not replace
this file.
"""

from __future__ import annotations

from core.schemas import Diagnosis, Transaction
from core.taxonomy import FailureClass

# --- Confidence levels -------------------------------------------------------
# Stated once, here, so the planner's thresholds are readable against them.

CONF_UNAMBIGUOUS_CODE = 0.95
"""The gateway code maps to exactly one class."""
CONF_AMBIGUOUS_RESOLVED = 0.85
"""An ambiguous code, disambiguated by a message keyword."""
CONF_AMBIGUOUS_DEFAULT = 0.55
"""An ambiguous code with no usable keyword; fell back to the likelier class."""
CONF_MESSAGE_ONLY = 0.45
"""Unknown code, classified from message text alone."""

#: Gateway codes that map to exactly one failure class.
UNAMBIGUOUS_CODES: dict[str, FailureClass] = {
    "GW_51": FailureClass.INSUFFICIENT_FUNDS,
    "GW_91": FailureClass.ISSUER_DOWN,
    "GW_TO": FailureClass.ISSUER_DOWN,
    "GW_54": FailureClass.CARD_EXPIRED,
    "GW_3DS": FailureClass.THREE_DS_TIMEOUT,
    "GW_ABD": FailureClass.CHECKOUT_ABANDONED,
    "GW_FRD": FailureClass.SUSPECTED_FRAUD,
    "GW_14": FailureClass.INVALID_ACCOUNT,
}

#: Codes that need the message to disambiguate.
#: `(keyword -> class, ...)` is tried in order; `default` applies if none match.
AMBIGUOUS_CODES: dict[str, dict] = {
    "GW_05": {
        "candidates": (
            (("insufficient", "balance", "funds", "low bal"), FailureClass.INSUFFICIENT_FUNDS),
        ),
        "default": FailureClass.DO_NOT_HONOUR,
    },
    "GW_MND": {
        "candidates": (
            (("revoked", "cancelled", "canceled", "withdrawn"), FailureClass.MANDATE_REVOKED),
            (("expired", "elapsed", "validity", "lapsed"), FailureClass.MANDATE_EXPIRED),
        ),
        "default": FailureClass.MANDATE_EXPIRED,
    },
}

#: Last-resort keyword matching when the code is unrecognised entirely.
#: Ordered most- to least-specific: `stolen` must beat `declined`.
MESSAGE_KEYWORDS: tuple[tuple[tuple[str, ...], FailureClass], ...] = (
    (("stolen", "fraud", "fraudulent"), FailureClass.SUSPECTED_FRAUD),
    (("no such account", "invalid account", "account not found"), FailureClass.INVALID_ACCOUNT),
    (("expired card", "card expiry", "card has expired"), FailureClass.CARD_EXPIRED),
    (("3ds", "otp", "authentication tim"), FailureClass.THREE_DS_TIMEOUT),
    (("mandate", "debit authorisation", "debit authorization"), FailureClass.MANDATE_EXPIRED),
    (("abandoned", "closed checkout", "session"), FailureClass.CHECKOUT_ABANDONED),
    (("insufficient", "low balance"), FailureClass.INSUFFICIENT_FUNDS),
    (("timeout", "unavailable", "gateway"), FailureClass.ISSUER_DOWN),
    (("do not honour", "do not honor"), FailureClass.DO_NOT_HONOUR),
)


def classify(txn: Transaction) -> Diagnosis:
    """Infer the failure class from observable signals only."""
    code = txn.failure_code.strip().upper()
    message = txn.failure_message.lower()

    if code in UNAMBIGUOUS_CODES:
        cls = UNAMBIGUOUS_CODES[code]
        return Diagnosis(
            txn_id=txn.txn_id,
            failure_class=cls,
            confidence=CONF_UNAMBIGUOUS_CODE,
            rule_id=f"code:{code}",
            rationale=f"Gateway code {code} maps uniquely to {cls.value}.",
        )

    if code in AMBIGUOUS_CODES:
        spec = AMBIGUOUS_CODES[code]
        for keywords, cls in spec["candidates"]:
            hit = next((k for k in keywords if k in message), None)
            if hit:
                return Diagnosis(
                    txn_id=txn.txn_id,
                    failure_class=cls,
                    confidence=CONF_AMBIGUOUS_RESOLVED,
                    rule_id=f"code:{code}+kw:{hit}",
                    rationale=(
                        f"Code {code} is ambiguous; message contains '{hit}', "
                        f"which resolves it to {cls.value}."
                    ),
                )
        cls = spec["default"]
        return Diagnosis(
            txn_id=txn.txn_id,
            failure_class=cls,
            confidence=CONF_AMBIGUOUS_DEFAULT,
            rule_id=f"code:{code}+default",
            rationale=(
                f"Code {code} is ambiguous and no keyword matched; fell back to "
                f"the likelier class {cls.value}. Low confidence by design."
            ),
        )

    for keywords, cls in MESSAGE_KEYWORDS:
        hit = next((k for k in keywords if k in message), None)
        if hit:
            return Diagnosis(
                txn_id=txn.txn_id,
                failure_class=cls,
                confidence=CONF_MESSAGE_ONLY,
                rule_id=f"kw:{hit}",
                rationale=(
                    f"Unrecognised code {code!r}; message keyword '{hit}' "
                    f"suggests {cls.value}."
                ),
            )

    return Diagnosis(
        txn_id=txn.txn_id,
        failure_class=None,
        confidence=0.0,
        rule_id="unclassified",
        rationale=(
            f"No rule matched code {code!r} or message {txn.failure_message!r}. "
            "Left undiagnosed rather than guessed; the planner must not auto-act."
        ),
    )


def classify_all(txns: list[Transaction]) -> dict[str, Diagnosis]:
    return {t.txn_id: classify(t) for t in txns}

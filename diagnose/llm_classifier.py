"""LLM classification — an upgrade on the rules path, never a dependency.

Three constraints from CLAUDE.md, enforced here rather than hoped for:

1. **Narrow use.** The LLM turns an unstructured `failure_message` into a
   structured class. It never sees policy, never proposes an action, and never
   reaches the executor.
2. **Rules-first.** It is only consulted for rows `diagnose.classifier` could
   not confidently handle. Everything else keeps the deterministic answer, so
   the LLM cannot regress rows the rules already got right.
3. **Degrades to rules.** Any failure — no API key, network error, refusal,
   unparseable output, a class outside the enum — returns the rules diagnosis
   and logs the degradation. `--no-llm` skips this module entirely.

The batch must run with the API down. That is the whole design.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from core.schemas import Diagnosis, Transaction
from core.taxonomy import FailureClass
from diagnose.classifier import CONF_AMBIGUOUS_DEFAULT, classify

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

#: Only rows at or below this rules-confidence are sent to the LLM. The
#: unambiguous-code path (0.95) and the keyword-resolved path (0.85) are already
#: right; spending tokens on them buys nothing and risks making them wrong.
ESCALATE_AT_OR_BELOW = CONF_AMBIGUOUS_DEFAULT

#: Confidence assigned to an accepted LLM answer. Deliberately below the
#: unambiguous-code path: the LLM read the same ambiguous text a human would,
#: so it should not outrank a definitive gateway code.
CONF_LLM = 0.75

SYSTEM_PROMPT = """You classify failed Indian D2C subscription payments.

Given a payment gateway's raw failure code and message, identify which failure \
class it represents. These are the only valid classes:

- ISSUER_DOWN: the issuing bank or gateway was unavailable or timed out.
- INSUFFICIENT_FUNDS: the account lacked balance.
- CARD_EXPIRED: the card's expiry date has passed.
- MANDATE_EXPIRED: an e-mandate lapsed on its own.
- MANDATE_REVOKED: the customer deliberately cancelled the mandate.
- 3DS_TIMEOUT: the customer did not complete OTP or 3-D Secure auth.
- CHECKOUT_ABANDONED: the customer left before authorising payment.
- DO_NOT_HONOUR: a generic issuer decline with no stated reason.
- SUSPECTED_FRAUD: fraud, stolen card, or risk-engine block.
- INVALID_ACCOUNT: the account or card number does not exist.

Two distinctions matter most, because the gateway codes are ambiguous:

- INSUFFICIENT_FUNDS vs DO_NOT_HONOUR: both are commonly returned as code 05. \
Choose INSUFFICIENT_FUNDS only when the text indicates balance. A bare \
"do not honour" with no reason is DO_NOT_HONOUR.
- MANDATE_EXPIRED vs MANDATE_REVOKED: "revoked", "cancelled", or "withdrawn" \
indicates the customer acted deliberately (REVOKED). "Expired", "elapsed", or \
"lapsed" indicates it ran out on its own (EXPIRED).

Report genuine uncertainty in your confidence score rather than guessing \
confidently. A wrong confident answer is worse than an honest low one, because \
downstream policy uses confidence to decide whether to involve a human."""


class _LlmVerdict(BaseModel):
    """Schema the model's response is constrained to."""

    failure_class: FailureClass = Field(
        description="The single best-matching failure class."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Honest confidence in this classification."
    )
    reasoning: str = Field(
        max_length=300, description="One sentence justifying the choice."
    )


@dataclass
class LlmStats:
    """What the LLM layer actually did. Reported so its value is measurable."""

    considered: int = 0
    escalated: int = 0
    accepted: int = 0
    degraded: int = 0
    degradation_reasons: dict[str, int] = field(default_factory=dict)

    def _degrade(self, reason: str) -> None:
        self.degraded += 1
        self.degradation_reasons[reason] = self.degradation_reasons.get(reason, 0) + 1


class LlmClassifier:
    """Wraps the rules classifier. Same signature, better coverage, or no worse."""

    def __init__(self, client=None, model: str = MODEL, enabled: bool = True):
        self.model = model
        self.stats = LlmStats()
        self._client = client
        self.enabled = enabled and (client is not None or self._can_authenticate())

        if enabled and not self.enabled:
            log.warning(
                "LLM layer requested but no credentials found; running rules-only. "
                "This is a supported mode, not an error."
            )

    @staticmethod
    def _can_authenticate() -> bool:
        """An unset ANTHROPIC_API_KEY does not prove there are no credentials —
        the SDK also resolves an `ant auth login` profile. Construct the client
        and let it tell us."""
        try:
            import anthropic

            anthropic.Anthropic()
            return True
        except Exception:  # noqa: BLE001 - any auth/import failure means rules-only
            return False

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def classify(self, txn: Transaction) -> Diagnosis:
        """Rules first. Escalate to the LLM only where rules were weak."""
        rules = classify(txn)
        self.stats.considered += 1

        if not self.enabled:
            return rules
        if rules.is_classified and rules.confidence > ESCALATE_AT_OR_BELOW:
            return rules

        self.stats.escalated += 1
        verdict = self._ask(txn)
        if verdict is None:
            return rules

        self.stats.accepted += 1
        return Diagnosis(
            txn_id=txn.txn_id,
            failure_class=verdict.failure_class,
            confidence=min(verdict.confidence, CONF_LLM),
            rule_id=f"llm:{self.model}",
            rationale=f"{verdict.reasoning} (rules said: {rules.rule_id})",
            source="llm",
        )

    def _ask(self, txn: Transaction) -> _LlmVerdict | None:
        """One constrained call. Returns `None` on any problem at all."""
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        # Stable prefix across every row in the batch.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_format=_LlmVerdict,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Gateway code: {txn.failure_code!r}\n"
                            f"Gateway message: {txn.failure_message!r}\n"
                            f"Payment method: {txn.method.value}\n"
                            f"Subscription: {txn.is_subscription}\n"
                            f"Mandate status: {txn.mandate_status.value}"
                        ),
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 - degrade on anything
            self.stats._degrade(type(exc).__name__)
            log.warning("LLM call failed for %s: %s", txn.txn_id, exc)
            return None

        # A refusal returns HTTP 200 with no usable content. Check before reading.
        if getattr(response, "stop_reason", None) == "refusal":
            self.stats._degrade("refusal")
            return None

        verdict = getattr(response, "parsed_output", None)
        if verdict is None:
            self.stats._degrade("unparseable")
            return None

        if verdict.failure_class not in set(FailureClass):
            # Structured outputs should make this impossible. Checked anyway,
            # because "should be impossible" is not a guarantee about money.
            self.stats._degrade("class_outside_enum")
            return None

        return verdict


def classify_all(
    txns: list[Transaction], classifier: LlmClassifier | None = None
) -> tuple[dict[str, Diagnosis], LlmStats]:
    """Drop-in replacement for `diagnose.classifier.classify_all`."""
    classifier = classifier or LlmClassifier()
    return {t.txn_id: classifier.classify(t) for t in txns}, classifier.stats

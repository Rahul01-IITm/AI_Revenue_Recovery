"""Customer-facing message copy, with a template fallback that always works.

The LLM writes friendlier copy than a template can, including Hinglish for
customers who prefer it. What it is never allowed to do is invent a fact: the
amount, the merchant, and the action all come from the transaction, and the
template path is always available if generation fails.

A message that promises something the policy engine did not authorise would be
a compliance problem, so the prompt is constrained to tone and phrasing only.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal

from core.schemas import Transaction
from core.taxonomy import Action, Channel

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

Language = Literal["en", "hinglish"]

#: Deterministic copy. Used with `--no-llm`, on any generation failure, and as
#: the reference the LLM is asked to rephrase rather than replace.
TEMPLATES: dict[Action, str] = {
    Action.REQUEST_INSTRUMENT_UPDATE: (
        "Hi! Your card on file has expired, so your {merchant} subscription "
        "payment of Rs.{amount} could not go through. Update your card here to "
        "stay subscribed: {link}"
    ),
    Action.REQUEST_MANDATE_RENEWAL: (
        "Hi! The auto-pay mandate for your {merchant} subscription is no longer "
        "active, so we could not collect Rs.{amount}. Renew it here: {link}"
    ),
    Action.SEND_PAYMENT_LINK: (
        "Hi! Your {merchant} payment of Rs.{amount} did not complete. You can "
        "finish it here: {link}"
    ),
    Action.NUDGE_WHATSAPP: (
        "Hi! Just a reminder that your {merchant} payment of Rs.{amount} is "
        "still pending. Complete it here: {link}"
    ),
    Action.NUDGE_EMAIL: (
        "Hi,\n\nYour recent {merchant} payment of Rs.{amount} did not go "
        "through. You can complete it here: {link}\n\nThanks."
    ),
    Action.OFFER_PARTIAL_PLAN: (
        "Hi! We noticed your {merchant} payment of Rs.{amount} did not go "
        "through. You can split it into smaller instalments here: {link}"
    ),
}

SYSTEM_PROMPT = """You write short payment-recovery messages for an Indian D2C \
subscription business.

You will be given a reference message containing the correct facts. Rewrite it \
to be warmer and more natural. Follow these rules exactly:

- Never change the amount, the merchant name, or the link. Copy them verbatim.
- Never invent a discount, a deadline, a penalty, a late fee, or any \
consequence of not paying. If it is not in the reference message, it does not \
go in yours.
- Never imply the customer has done something wrong.
- Keep it under 320 characters for WhatsApp and SMS, under 600 for email.
- No emoji unless the channel is WhatsApp, and at most one.
- Include exactly one call to action.

If asked for Hinglish, write natural Roman-script Hindi-English as urban Indian \
customers actually text — not translated-sounding formal Hindi. Keep payment \
terms in English. Keep the same facts and the same constraints."""


def render_template(
    txn: Transaction, action: Action, link: str = "{link}", merchant: str = ""
) -> str:
    """Deterministic copy. Never fails, never needs a network."""
    template = TEMPLATES.get(action)
    if template is None:
        return ""
    return template.format(
        merchant=merchant or _merchant_name(txn),
        amount=f"{txn.amount:,.0f}",
        link=link,
    )


def _merchant_name(txn: Transaction) -> str:
    return txn.merchant_id.removeprefix("mrc_").replace("_", " ").title()


class CopyWriter:
    """Generates message copy, falling back to the template on any problem."""

    def __init__(self, client=None, model: str = MODEL, enabled: bool = True):
        self.model = model
        self._client = client
        self.enabled = enabled and (client is not None or self._can_authenticate())
        self.generated = 0
        self.fell_back = 0

    @staticmethod
    def _can_authenticate() -> bool:
        try:
            import anthropic

            anthropic.Anthropic()
            return True
        except Exception:  # noqa: BLE001
            return False

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def write(
        self,
        txn: Transaction,
        action: Action,
        channel: Channel,
        language: Language = "en",
        link: str = "https://rzp.io/i/demo",
    ) -> str:
        """Copy for one outbound action. Always returns something sendable."""
        reference = render_template(txn, action, link=link)
        if not reference:
            return ""
        if not self.enabled:
            return reference

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Channel: {channel.value}\n"
                            f"Language: {'Hinglish' if language == 'hinglish' else 'English'}\n"
                            f"Reference message:\n{reference}\n\n"
                            "Reply with only the rewritten message."
                        ),
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("copy generation failed for %s: %s", txn.txn_id, exc)
            self.fell_back += 1
            return reference

        if getattr(response, "stop_reason", None) == "refusal":
            self.fell_back += 1
            return reference

        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()

        if not self._is_safe(text, txn, link):
            # The model changed a fact or dropped the link. Template wins.
            log.warning("generated copy failed the fact check for %s", txn.txn_id)
            self.fell_back += 1
            return reference

        self.generated += 1
        return text

    @staticmethod
    def _is_safe(text: str, txn: Transaction, link: str) -> bool:
        """Cheap, mechanical checks. Not a substitute for the prompt rules —
        a second line of defence, because generated text goes to real people."""
        if not text or len(text) > 900:
            return False
        if link not in text:
            return False
        if f"{txn.amount:,.0f}" not in text and f"{txn.amount:.0f}" not in text:
            return False
        forbidden = (
            "late fee", "penalty", "legal action", "suspend", "discount",
            "offer expires", "final notice", "overdue charges",
        )
        return not any(word in text.lower() for word in forbidden)

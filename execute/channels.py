"""Outbound channel adapters, and the per-message cost that makes uplift net.

Sending is simulated: no real message is dispatched to anyone. What is *not*
simulated is the cost accounting — every message that would have been sent is
priced, so the headline recovery number is net of what it took to earn it.
"""

from __future__ import annotations

from decimal import Decimal

from core.schemas import Transaction
from core.taxonomy import Action, Channel
from policy.limits import MESSAGE_COST

#: Which channel each outbound action prefers, in order.
ACTION_CHANNEL_PREFERENCE: dict[Action, tuple[Channel, ...]] = {
    Action.NUDGE_WHATSAPP: (Channel.WHATSAPP, Channel.SMS, Channel.EMAIL),
    Action.NUDGE_EMAIL: (Channel.EMAIL, Channel.WHATSAPP),
    Action.SEND_PAYMENT_LINK: (Channel.WHATSAPP, Channel.SMS, Channel.EMAIL),
    Action.REQUEST_INSTRUMENT_UPDATE: (Channel.EMAIL, Channel.WHATSAPP),
    Action.REQUEST_MANDATE_RENEWAL: (Channel.EMAIL, Channel.WHATSAPP),
    Action.OFFER_PARTIAL_PLAN: (Channel.EMAIL, Channel.WHATSAPP),
}


def choose_channel(txn: Transaction, action: Action) -> Channel | None:
    """First channel the action prefers that the customer also accepts."""
    for channel in ACTION_CHANNEL_PREFERENCE.get(action, ()):
        if channel in txn.channel_prefs:
            return channel
    return None


def cost_of(channel: Channel | None) -> Decimal:
    if channel is None:
        return Decimal("0")
    return MESSAGE_COST.get(channel.value, Decimal("0"))


def send(txn: Transaction, action: Action, channel: Channel, body: str) -> str:
    """Simulate delivery. Returns a provider-style reference for the audit log.

    Deliberately does no network I/O. The customer's *response* is what the
    outcome simulator models; delivery itself is assumed to succeed, which is
    a stated assumption rather than a hidden one.
    """
    return f"sent:{channel.value.lower()}:{txn.txn_id}:{action.value.lower()}"

"""The guardrails, as constants.

Transcribed verbatim from CLAUDE.md. These are hard-coded, never prompted: an
LLM is not trusted to respect them, and nothing in this repo asks one to.

Every constant here has a test in `tests/test_policy.py` that proves the rule
enforcing it actually fires.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MAX_RETRIES_PER_TXN = 2
"""Total charge re-attempts per transaction, including retries that happened
before the batch reached us (`Transaction.retry_count`)."""

MAX_CUSTOMER_MESSAGES_PER_TXN = 2

MAX_MESSAGES_PER_CUSTOMER_7D = 3
"""Cross-transaction. A customer with four failed subscriptions must not get
four independent dunning sequences — this is the anti-spam guardrail."""

RECOVERY_WINDOW = timedelta(hours=72)
"""Measured from `Transaction.timestamp`. After this, STOP."""

QUIET_HOURS_START = 21
QUIET_HOURS_END = 9
"""21:00-09:00 IST. No outbound contact. Deferred, not cancelled."""

HUMAN_APPROVAL_THRESHOLD = Decimal("50000.00")
"""At or above this value the agent may not act on its own."""

#: Per-message cost by channel, so recovery is reported net rather than gross.
#: Indicative Indian rates: WhatsApp utility template, transactional SMS, email.
MESSAGE_COST: dict[str, Decimal] = {
    "WHATSAPP": Decimal("0.35"),
    "SMS": Decimal("0.18"),
    "EMAIL": Decimal("0.02"),
}


def is_quiet_hours(dt) -> bool:
    """True if `dt` falls inside the quiet window, evaluated in IST.

    Timestamps elsewhere in the system are UTC; converting here rather than at
    the call sites means a caller cannot forget to.
    """
    hour = dt.astimezone(IST).hour
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


def next_allowed_contact_time(dt):
    """The earliest time at or after `dt` that is outside quiet hours."""
    local = dt.astimezone(IST)
    if not is_quiet_hours(local):
        return dt

    target = local.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)
    if local.hour >= QUIET_HOURS_START:
        target += timedelta(days=1)
    return target.astimezone(dt.tzinfo)

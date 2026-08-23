"""Razorpay test-mode adapter, with an offline path that always works.

Where real test-mode endpoints are available we call them and log the real
response. Where they are not — and during the demo, where the network may not
be — the adapter runs offline and says so in the audit trail rather than
pretending a call happened.

`RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in the environment switch it on. No
key is ever read from a committed file; `.env` is gitignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ApiResponse:
    ok: bool
    reference: str
    mode: str
    """`test` when a real Razorpay test-mode call was made, `offline` when the
    adapter stubbed it. Recorded so no one mistakes one for the other."""
    detail: str = ""


class RazorpayAdapter:
    """Thin wrapper. Deliberately small: the interesting logic is upstream."""

    def __init__(self, key_id: str | None = None, key_secret: str | None = None):
        self.key_id = key_id or os.environ.get("RAZORPAY_KEY_ID")
        self.key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET")

    @property
    def live(self) -> bool:
        """True when real test-mode credentials are present."""
        return bool(self.key_id and self.key_secret)

    @property
    def mode(self) -> str:
        return "test" if self.live else "offline"

    def retry_payment(self, txn_id: str, amount: Decimal) -> ApiResponse:
        """Re-attempt a charge.

        Offline, this reports the attempt without asserting an outcome — whether
        the money arrives is the simulator's call, not the adapter's.
        """
        if not self.live:
            return ApiResponse(
                ok=True,
                reference=f"offline_retry_{txn_id}",
                mode="offline",
                detail="No Razorpay credentials; attempt recorded, not sent.",
            )
        return self._call("orders.create", txn_id, amount)

    def create_payment_link(self, txn_id: str, amount: Decimal) -> ApiResponse:
        if not self.live:
            return ApiResponse(
                ok=True,
                reference=f"offline_link_{txn_id}",
                mode="offline",
                detail="No Razorpay credentials; link not created.",
            )
        return self._call("payment_link.create", txn_id, amount)

    def _call(self, endpoint: str, txn_id: str, amount: Decimal) -> ApiResponse:
        """Real test-mode call.

        Imports the SDK lazily so the package stays an optional dependency and
        the deterministic path runs with nothing installed. A failure here is
        returned, not raised: the executor's backoff-and-quarantine handling
        owns that decision so one bad response cannot stop the batch.
        """
        try:
            import razorpay  # type: ignore
        except ImportError:
            return ApiResponse(
                ok=False,
                reference="",
                mode="offline",
                detail="razorpay SDK not installed; falling back to offline.",
            )

        try:
            client = razorpay.Client(auth=(self.key_id, self.key_secret))
            paise = int(amount * 100)
            if endpoint == "orders.create":
                resp = client.order.create(
                    {"amount": paise, "currency": "INR", "receipt": txn_id}
                )
            else:
                resp = client.payment_link.create(
                    {"amount": paise, "currency": "INR", "reference_id": txn_id}
                )
            return ApiResponse(
                ok=True, reference=str(resp.get("id", "")), mode="test",
                detail=endpoint,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the executor
            return ApiResponse(
                ok=False, reference="", mode="test",
                detail=f"{type(exc).__name__}: {exc}",
            )

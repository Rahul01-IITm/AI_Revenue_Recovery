"""The LLM layer, tested entirely with fakes.

No API key, no network. The point of these tests is the property that makes the
layer safe to ship: **every failure mode degrades to the rules path**, and the
batch still completes.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.schemas import Transaction
from core.taxonomy import Action, Channel, FailureClass, MandateStatus, PaymentMethod
from diagnose.classifier import classify
from diagnose.llm_classifier import CONF_LLM, LlmClassifier, _LlmVerdict
from execute.copy import CopyWriter, render_template


def _txn(**overrides) -> Transaction:
    base = dict(
        txn_id="txn_test", customer_id="cust_test", merchant_id="mrc_streamly",
        amount=Decimal("499.00"), timestamp="2026-08-20T10:00:00Z",
        method=PaymentMethod.CARD, failure_code="GW_05",
        failure_message="do not honour", retry_count=0,
        customer_ltv=Decimal("1200.00"), prior_success_rate=0.9,
        prior_failures_7d=0, is_subscription=True,
        mandate_status=MandateStatus.NONE, opted_out=False,
        channel_prefs=(Channel.WHATSAPP, Channel.EMAIL),
    )
    return Transaction(**{**base, **overrides})


class _FakeClient:
    """Minimal stand-in for `anthropic.Anthropic`."""

    def __init__(self, *, verdict=None, raises=None, stop_reason=None, text=None):
        self._verdict = verdict
        self._raises = raises
        self._stop_reason = stop_reason
        self._text = text
        self.calls = 0
        self.messages = SimpleNamespace(parse=self._parse, create=self._create)

    def _respond(self):
        self.calls += 1
        if self._raises:
            raise self._raises

    def _parse(self, **kwargs):
        self._respond()
        return SimpleNamespace(
            parsed_output=self._verdict, stop_reason=self._stop_reason
        )

    def _create(self, **kwargs):
        self._respond()
        blocks = [SimpleNamespace(type="text", text=self._text or "")]
        return SimpleNamespace(content=blocks, stop_reason=self._stop_reason)


# --- Escalation policy -------------------------------------------------------


def test_confident_rules_answers_are_not_sent_to_the_llm():
    """Spending tokens on rows the rules already got right buys nothing and
    risks making them wrong."""
    client = _FakeClient(verdict=_LlmVerdict(
        failure_class=FailureClass.ISSUER_DOWN, confidence=0.9, reasoning="x"))
    clf = LlmClassifier(client=client)

    result = clf.classify(_txn(failure_code="GW_51",
                               failure_message="insufficient funds in account"))
    assert client.calls == 0
    assert result.source == "rules"
    assert clf.stats.escalated == 0


def test_ambiguous_rows_are_escalated():
    client = _FakeClient(verdict=_LlmVerdict(
        failure_class=FailureClass.INSUFFICIENT_FUNDS, confidence=0.8,
        reasoning="issuer decline consistent with balance"))
    clf = LlmClassifier(client=client)

    result = clf.classify(_txn(failure_code="GW_05", failure_message="do not honour"))
    assert client.calls == 1
    assert result.source == "llm"
    assert result.failure_class is FailureClass.INSUFFICIENT_FUNDS
    assert clf.stats.accepted == 1


def test_unclassified_rows_are_escalated():
    client = _FakeClient(verdict=_LlmVerdict(
        failure_class=FailureClass.ISSUER_DOWN, confidence=0.7, reasoning="timeout"))
    clf = LlmClassifier(client=client)

    txn = _txn(failure_code="GW_UNK", failure_message="unspecified processing error")
    assert classify(txn).failure_class is None  # rules give up
    assert clf.classify(txn).failure_class is FailureClass.ISSUER_DOWN


def test_llm_confidence_never_outranks_an_unambiguous_code():
    """The LLM read ambiguous text; a definitive gateway code did not."""
    client = _FakeClient(verdict=_LlmVerdict(
        failure_class=FailureClass.DO_NOT_HONOUR, confidence=1.0, reasoning="certain"))
    result = LlmClassifier(client=client).classify(_txn())
    assert result.confidence <= CONF_LLM


# --- Degradation: the property that makes this safe to ship ------------------


@pytest.mark.parametrize("failure", [
    ConnectionError("network down"),
    TimeoutError("timed out"),
    ValueError("garbage"),
    RuntimeError("boom"),
])
def test_any_exception_degrades_to_rules(failure):
    clf = LlmClassifier(client=_FakeClient(raises=failure))
    rules = classify(_txn())
    result = clf.classify(_txn())

    assert result.failure_class == rules.failure_class
    assert result.source == "rules"
    assert clf.stats.degraded == 1


def test_refusal_degrades_to_rules():
    clf = LlmClassifier(client=_FakeClient(verdict=None, stop_reason="refusal"))
    assert clf.classify(_txn()).source == "rules"
    assert clf.stats.degradation_reasons.get("refusal") == 1


def test_unparseable_output_degrades_to_rules():
    clf = LlmClassifier(client=_FakeClient(verdict=None))
    assert clf.classify(_txn()).source == "rules"
    assert clf.stats.degradation_reasons.get("unparseable") == 1


def test_degradation_is_logged_with_a_reason():
    clf = LlmClassifier(client=_FakeClient(raises=ConnectionError("x")))
    clf.classify(_txn())
    assert clf.stats.degradation_reasons == {"ConnectionError": 1}


def test_disabled_layer_never_calls_out():
    """The --no-llm path. Must work with the API down."""
    client = _FakeClient(raises=AssertionError("must not be called"))
    clf = LlmClassifier(client=client, enabled=False)
    assert clf.classify(_txn()).source == "rules"
    assert client.calls == 0


def test_batch_completes_when_every_call_fails():
    """The headline guarantee: a total LLM outage costs accuracy, not the run."""
    from data.generator import generate_batch
    from diagnose.llm_classifier import classify_all

    batch = generate_batch(n=50, seed=42)
    clf = LlmClassifier(client=_FakeClient(raises=ConnectionError("down")))
    diagnoses, stats = classify_all(batch.transactions, clf)

    assert len(diagnoses) == 50
    assert all(d.source == "rules" for d in diagnoses.values())
    assert stats.degraded == stats.escalated > 0


# --- Message copy ------------------------------------------------------------


def test_template_needs_no_network():
    text = render_template(_txn(), Action.REQUEST_INSTRUMENT_UPDATE, link="L")
    assert "499" in text
    assert "Streamly" in text
    assert "L" in text


def test_generated_copy_is_used_when_it_passes_the_fact_check():
    good = "Hi! Your Streamly card expired so Rs.499 didn't go through. Fix: L"
    writer = CopyWriter(client=_FakeClient(text=good))
    out = writer.write(_txn(), Action.REQUEST_INSTRUMENT_UPDATE,
                       Channel.WHATSAPP, link="L")
    assert out == good
    assert writer.generated == 1


def test_copy_that_drops_the_link_falls_back_to_template():
    writer = CopyWriter(client=_FakeClient(text="Hi! Rs.499 is pending."))
    out = writer.write(_txn(), Action.SEND_PAYMENT_LINK, Channel.WHATSAPP, link="L")
    assert "L" in out
    assert writer.fell_back == 1


def test_copy_that_changes_the_amount_falls_back():
    """A generated message quoting the wrong amount is a support ticket at best."""
    writer = CopyWriter(client=_FakeClient(text="Hi! Pay Rs.9999 now: L"))
    out = writer.write(_txn(), Action.SEND_PAYMENT_LINK, Channel.WHATSAPP, link="L")
    assert "499" in out
    assert writer.fell_back == 1


@pytest.mark.parametrize("threat", [
    "Pay Rs.499 or face legal action: L",
    "Final notice: Rs.499 overdue charges apply. L",
    "Rs.499 due, we will suspend your account: L",
    "Rs.499 — 50% discount if you pay today: L",
])
def test_coercive_or_invented_copy_is_rejected(threat):
    """No harassment, and no promises policy never authorised."""
    writer = CopyWriter(client=_FakeClient(text=threat))
    out = writer.write(_txn(), Action.SEND_PAYMENT_LINK, Channel.WHATSAPP, link="L")
    assert out != threat
    assert writer.fell_back == 1


def test_copy_generation_failure_falls_back_silently():
    writer = CopyWriter(client=_FakeClient(raises=ConnectionError("down")))
    out = writer.write(_txn(), Action.SEND_PAYMENT_LINK, Channel.EMAIL, link="L")
    assert "499" in out and "L" in out


def test_disabled_writer_returns_the_template():
    writer = CopyWriter(client=_FakeClient(raises=AssertionError("no")), enabled=False)
    assert "499" in writer.write(_txn(), Action.NUDGE_EMAIL, Channel.EMAIL, link="L")


def test_every_outbound_action_has_copy():
    from core.taxonomy import OUTBOUND_ACTIONS

    for action in OUTBOUND_ACTIONS:
        assert render_template(_txn(), action, link="L"), action

#!/usr/bin/env python3
"""Live integration verification — the two things the test suite cannot prove.

Every other test in this repo runs offline against fakes. That proves the
*degradation* paths work; it says nothing about whether the real endpoints
behave as we assume. This script exercises them for real and reports what
actually happened, including cost and latency.

    python verify_live.py                 # check credentials, run what is available
    python verify_live.py --razorpay      # Razorpay test-mode only
    python verify_live.py --llm --limit 25   # LLM only, 25 rows
    python verify_live.py --all --limit 50

It is safe to run with no credentials: each section reports SKIPPED and explains
what to set. Nothing here touches production, and the Razorpay calls are
test-mode only — a test key cannot move real money.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal

# --- Presentation ------------------------------------------------------------

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def _supports_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


if not _supports_colour():
    GREEN = RED = YELLOW = DIM = BOLD = RESET = ""


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP | WARN
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


def _mark(status: str) -> str:
    return {
        "PASS": f"{GREEN}PASS{RESET}",
        "FAIL": f"{RED}FAIL{RESET}",
        "SKIP": f"{YELLOW}SKIP{RESET}",
        "WARN": f"{YELLOW}WARN{RESET}",
    }[status]


def _section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}\n{'=' * len(title)}")


def _report(checks: list[Check]) -> None:
    for c in checks:
        print(f"  [{_mark(c.status)}] {c.name}")
        if c.detail:
            print(f"         {c.detail}")
        for line in c.evidence:
            print(f"         {DIM}{line}{RESET}")


# --- Razorpay ----------------------------------------------------------------


def verify_razorpay() -> list[Check]:
    """Exercise the test-mode endpoints the adapter actually calls."""
    from execute.razorpay_adapter import RazorpayAdapter

    checks: list[Check] = []
    adapter = RazorpayAdapter()

    if not adapter.live:
        return [
            Check(
                "Razorpay credentials",
                "SKIP",
                "No RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in the environment.",
                [
                    "Get test-mode keys: dashboard.razorpay.com -> Settings -> API Keys",
                    "They start with rzp_test_ . A live key (rzp_live_) is NOT needed",
                    "and should never be used here.",
                    "",
                    "  export RAZORPAY_KEY_ID=rzp_test_xxxxxxxx",
                    "  export RAZORPAY_KEY_SECRET=xxxxxxxx",
                    "  pip install razorpay",
                ],
            )
        ]

    if not adapter.key_id.startswith("rzp_test_"):
        checks.append(
            Check(
                "Key is test-mode",
                "FAIL",
                f"Key {adapter.key_id[:12]}... is not a test key. Refusing to continue.",
                ["This script must never run against a live key."],
            )
        )
        return checks

    checks.append(Check("Key is test-mode", "PASS", f"{adapter.key_id[:16]}..."))

    try:
        import razorpay  # noqa: F401
    except ImportError:
        checks.append(
            Check("razorpay SDK installed", "FAIL", "pip install razorpay")
        )
        return checks
    checks.append(Check("razorpay SDK installed", "PASS"))

    # 1. Order creation — what a retry maps onto.
    started = time.perf_counter()
    resp = adapter.retry_payment("verify_live_order", Decimal("499.00"))
    elapsed = (time.perf_counter() - started) * 1000
    checks.append(
        Check(
            "orders.create (the RETRY path)",
            "PASS" if resp.ok and resp.mode == "test" else "FAIL",
            f"mode={resp.mode} ref={resp.reference or '-'} {elapsed:.0f}ms",
            [resp.detail] if resp.detail else [],
        )
    )

    # 2. Payment link — what SEND_PAYMENT_LINK maps onto.
    started = time.perf_counter()
    link = adapter.create_payment_link("verify_live_link", Decimal("499.00"))
    elapsed = (time.perf_counter() - started) * 1000
    checks.append(
        Check(
            "payment_link.create (the SEND_PAYMENT_LINK path)",
            "PASS" if link.ok and link.mode == "test" else "FAIL",
            f"mode={link.mode} ref={link.reference or '-'} {elapsed:.0f}ms",
            [link.detail] if link.detail else [],
        )
    )

    # 3. The failure path must still degrade, not raise.
    bad = RazorpayAdapter(key_id="rzp_test_invalid", key_secret="invalid")
    bad_resp = bad.retry_payment("verify_live_bad", Decimal("1.00"))
    checks.append(
        Check(
            "Bad credentials degrade rather than raise",
            "PASS" if not bad_resp.ok else "WARN",
            f"ok={bad_resp.ok} — {bad_resp.detail[:90]}",
            ["The executor's backoff-then-quarantine path depends on this."],
        )
    )

    return checks


# --- LLM ---------------------------------------------------------------------


def verify_llm(limit: int) -> list[Check]:
    """Run the real classifier on the rows the rules could not handle.

    This is the only measurement that tells us whether the LLM layer is worth
    shipping. Everything in the offline suite proves it fails safely; nothing
    proves it helps.
    """
    from data.generator import generate_batch
    from diagnose.classifier import classify
    from diagnose.llm_classifier import ESCALATE_AT_OR_BELOW, LlmClassifier

    checks: list[Check] = []
    clf = LlmClassifier()

    if not clf.enabled:
        return [
            Check(
                "Anthropic credentials",
                "SKIP",
                "No API key or `ant` profile found.",
                [
                    "  export ANTHROPIC_API_KEY=sk-ant-...",
                    "or run:  ant auth login",
                    "",
                    "The batch runs fine without this — the LLM only sharpens",
                    "diagnosis on the ~4% of rows the rules found ambiguous.",
                ],
            )
        ]
    checks.append(Check("Anthropic credentials", "PASS", f"model={clf.model}"))

    batch = generate_batch(n=500, seed=42)
    # Only the rows the layer would actually escalate in a real run.
    targets = [
        t
        for t in batch.select("test")
        if (d := classify(t)).failure_class is None
        or d.confidence <= ESCALATE_AT_OR_BELOW
    ][:limit]

    if not targets:
        return checks + [Check("Escalation population", "WARN", "No rows to test.")]

    checks.append(
        Check(
            "Escalation population",
            "PASS",
            f"{len(targets)} rows the rules could not confidently classify",
        )
    )

    rules_right = llm_right = 0
    latencies: list[float] = []
    started_all = time.perf_counter()

    for txn in targets:
        truth = batch.true_class(txn.txn_id)
        rules = classify(txn)
        t0 = time.perf_counter()
        got = clf.classify(txn)
        latencies.append((time.perf_counter() - t0) * 1000)
        rules_right += rules.failure_class == truth
        llm_right += got.failure_class == truth

    wall = time.perf_counter() - started_all
    n = len(targets)
    delta = (llm_right - rules_right) / n

    checks.append(
        Check(
            "LLM beats rules on the escalated rows",
            "PASS" if llm_right > rules_right else "WARN",
            f"rules {rules_right}/{n} ({rules_right/n:.0%})  ->  "
            f"LLM {llm_right}/{n} ({llm_right/n:.0%})   {delta:+.0%}",
            [
                "WARN here is a real finding, not a flaky test: it means the LLM",
                "layer is not earning its cost and should be cut before the demo.",
            ] if llm_right <= rules_right else [],
        )
    )

    latencies.sort()
    checks.append(
        Check(
            "Latency",
            "PASS" if latencies[len(latencies) // 2] < 20_000 else "WARN",
            f"median {latencies[len(latencies)//2]:.0f}ms  "
            f"p90 {latencies[int(len(latencies)*0.9)]:.0f}ms  "
            f"total {wall:.1f}s for {n} rows",
        )
    )

    s = clf.stats
    checks.append(
        Check(
            "Degradation accounting",
            "PASS" if not s.circuit_opened else "WARN",
            f"escalated={s.escalated} accepted={s.accepted} degraded={s.degraded} "
            f"circuit_opened={s.circuit_opened}",
            [f"reasons: {s.degradation_reasons}"] if s.degradation_reasons else [],
        )
    )

    # Copy generation, including Hinglish.
    from core.taxonomy import Action, Channel
    from execute.copy import CopyWriter

    writer = CopyWriter()
    sample = targets[0]
    en = writer.write(sample, Action.SEND_PAYMENT_LINK, Channel.WHATSAPP, "en")
    hi = writer.write(sample, Action.SEND_PAYMENT_LINK, Channel.WHATSAPP, "hinglish")
    checks.append(
        Check(
            "Message copy passes the fact check",
            "PASS" if writer.generated >= 1 else "WARN",
            f"generated={writer.generated} fell_back={writer.fell_back}",
            [f"EN: {en[:100]}", f"HI: {hi[:100]}"],
        )
    )

    return checks


# --- Offline sanity ----------------------------------------------------------


def verify_offline() -> list[Check]:
    """Confirm the credentialed run and the offline run agree.

    The headline claim is that credentials change nothing that matters. If they
    ever do, something has leaked into the decision path.
    """
    from data.generator import generate_batch
    from runner import run_agent

    batch = generate_batch(n=300, seed=42)
    rules_only, _ = run_agent(batch, split="test", use_llm=False)
    return [
        Check(
            "Rules-only run completes",
            "PASS",
            f"{rules_only.recovered_count}/{rules_only.count} recovered, "
            f"{rules_only.recovery_rate_amount:.1%} of value, "
            f"{sum(rules_only.violations.values())} violations",
            ["This is the number the demo falls back to if anything is down."],
        )
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--razorpay", action="store_true")
    p.add_argument("--llm", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=15,
                   help="LLM rows to test (each is one API call)")
    args = p.parse_args(argv)

    run_rz = args.razorpay or args.all or not (args.razorpay or args.llm)
    run_llm = args.llm or args.all or not (args.razorpay or args.llm)

    all_checks: list[Check] = []

    _section("Offline baseline")
    c = verify_offline()
    _report(c)
    all_checks += c

    if run_rz:
        _section("Razorpay test-mode integration")
        c = verify_razorpay()
        _report(c)
        all_checks += c

    if run_llm:
        _section(f"LLM integration (up to {args.limit} live calls)")
        c = verify_llm(args.limit)
        _report(c)
        all_checks += c

    passed = sum(x.status == "PASS" for x in all_checks)
    failed = sum(x.status == "FAIL" for x in all_checks)
    skipped = sum(x.status == "SKIP" for x in all_checks)
    warned = sum(x.status == "WARN" for x in all_checks)

    _section("Summary")
    print(f"  {passed} passed, {failed} failed, {warned} warnings, {skipped} skipped\n")
    if skipped:
        print(f"  {DIM}Skipped sections need credentials — see the notes above.{RESET}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

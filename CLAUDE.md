# CLAUDE.md — Revenue Recovery Agent (Razorpay Hackathon, Track 03)

## What we are building

An agent that takes a batch of failed / at-risk payment records, diagnoses **why** each
one failed, chooses a **bounded** recovery intervention, executes it, and reports **how
much money came back** against a do-nothing baseline.

The deliverable is a **product with measured outcomes**, not a model and not a chatbot.
If a design choice trades model sophistication for a cleaner measured result, take the
measured result.

**Deadline: 5 September 2026.** Today is 21 August. ~15 working days.

## The single sentence the demo has to earn

> "Across 500 at-risk transactions worth ₹12.4L, our agent recovered ₹4.1L (33%).
> Doing nothing recovered ₹1.4L (11%). Every decision is logged, capped, and reversible."

Everything in this repo exists to make that sentence true and verifiable. The baseline
comparison is the winning slide — build the baseline **first**, before any intelligence.

## The judging bar (verbatim from the brief)

> Don't just identify the problem. Show measured money recovered across a batch, with
> compliant escalation, stopping rules, and an audit trail.

Four hard requirements decomposed:
1. **Measured money recovered across a batch** — batch, not a cherry-picked case. Report
   aggregate ₹ and rate, plus per-failure-class breakdown.
2. **Compliant escalation** — no harassment. Message caps, quiet hours, opt-out honoured,
   escalation ladder that a real collections/RBI-aware reviewer would accept.
3. **Stopping rules** — the agent must be able to give up, and must log why it gave up.
4. **Audit trail** — every decision has an id, a timestamp, an input snapshot, a reason,
   a policy verdict, and an outcome. Reconstructable after the fact.

## Architecture

```
Batch of at-risk transactions (synthetic, 500+)
        │
        ▼
[1] Ingest & normalise      → canonical Transaction schema
        │
        ▼
[2] Diagnosis layer         → failure class + recoverability score + confidence
        │
        ▼
[3] Policy engine (GATE)    → hard rules; can VETO or DOWNGRADE any proposed action
        │
        ▼
[4] Intervention planner    → chooses action + timing from the ALLOWED set only
        │
        ▼
[5] Executor                → Razorpay test-mode API / simulated channel adapters
        │
        ▼
[6] Outcome simulator       → probabilistic, calibrated, seeded, documented
        │
        ▼
[7] Ledger + audit store    → append-only, one row per decision and per outcome
        │
        ▼
[8] Dashboard               → recovery vs baseline, funnel, exceptions
```

**The policy engine sits between the AI and the money. That ordering is the whole point
of the track.** The planner proposes; the policy engine disposes. Never let an LLM call
the executor directly.

## Failure taxonomy → intervention map

This is the core domain logic. Distinct failure classes must lead to *visibly different*
behaviour, otherwise the agent is just a retry loop with extra steps.

| Failure class | Recoverable? | Action | Timing | Notes |
|---|---|---|---|---|
| `ISSUER_DOWN` / gateway timeout | High | `RETRY` | T+2h, then T+6h | Pure infra; retry is free money |
| `INSUFFICIENT_FUNDS` | Medium | `RETRY_SALARY_ALIGNED` | Next payday window / T+72h | Do NOT retry immediately — burns issuer trust |
| `CARD_EXPIRED` | Medium | `REQUEST_INSTRUMENT_UPDATE` | Immediate | Retry is guaranteed to fail; must involve customer |
| `MANDATE_EXPIRED` / `MANDATE_REVOKED` | Medium | `REQUEST_MANDATE_RENEWAL` | Immediate | Subscription path |
| `3DS_TIMEOUT` / auth abandoned | High | `SEND_PAYMENT_LINK` | T+30m | Customer intent was present; warm lead |
| `CHECKOUT_ABANDONED` | Medium | `NUDGE` then `PAYMENT_LINK` | T+1h, T+24h | Cart still valid check |
| `DO_NOT_HONOUR` | Low | `SINGLE_RETRY` then stop | T+24h | Ambiguous issuer response |
| `SUSPECTED_FRAUD` / stolen card | **Never** | `STOP` + flag | — | Hard block. Retrying is a compliance incident |
| `INVALID_ACCOUNT` | Never | `STOP` | — | Terminal |
| `B2B_INVOICE_OVERDUE` | High | escalation ladder | D+3 / D+10 / D+21 | Reminder → statement → human owner |

Allowed action enum — the planner may emit nothing outside this set:

```
RETRY | RETRY_SALARY_ALIGNED | SEND_PAYMENT_LINK | NUDGE_WHATSAPP | NUDGE_EMAIL
REQUEST_INSTRUMENT_UPDATE | REQUEST_MANDATE_RENEWAL | OFFER_PARTIAL_PLAN
ESCALATE_HUMAN | STOP
```

## Guardrails (hard-coded, not prompted)

These are enforced in code in the policy engine. An LLM is never trusted to respect them.

```
MAX_RETRIES_PER_TXN           = 2
MAX_CUSTOMER_MESSAGES_PER_TXN = 2
MAX_MESSAGES_PER_CUSTOMER_7D  = 3        # cross-transaction, prevents spam
RECOVERY_WINDOW               = 72h      # after this, STOP
QUIET_HOURS                   = 21:00–09:00 IST  # no outbound contact
HUMAN_APPROVAL_THRESHOLD      = ₹50,000  # above this, ESCALATE_HUMAN, do not auto-act
NEVER_RETRY                   = {SUSPECTED_FRAUD, INVALID_ACCOUNT, CHARGEBACK_OPEN}
OPT_OUT                       = absolute, checked before every outbound action
IDEMPOTENCY                   = every executed action carries a key; replay is a no-op
```

Every guardrail must have a test that proves it fires, and at least one must fire
visibly during the demo run.

## Data

Synthetic, generated by us, **seeded** (`SEED=42`) so runs are reproducible.

- 500–2000 transactions. Start at 500; scale only once the pipeline is stable.
- Realistic failure-class distribution — insufficient funds and issuer downtime should
  dominate; fraud should be rare (~2%). A uniform distribution looks fake to judges.
- Include awkward cases on purpose: repeat offenders, opted-out customers, a ₹1.2L
  transaction, a customer with 4 failures in a week, one already-chargebacked record.
- **Held-out split.** Tune on train, report numbers on test only. Never report a number
  computed on data we tuned against.

Fields: `txn_id, customer_id, merchant_id, amount, currency, timestamp, method,
failure_code, failure_message, retry_count, customer_ltv, prior_success_rate,
prior_failures_7d, is_subscription, mandate_status, opted_out, channel_prefs`

## Outcome simulation — read this before writing it

The single biggest credibility risk in this project. If the simulator is tuned so our
agent wins, the whole result is circular and a sharp judge will catch it in one question.

Rules:
- Recovery probabilities are set **per (failure_class, action, timing)** from stated
  assumptions, written down in `SIMULATION_ASSUMPTIONS.md` **before** the agent is built.
- The simulator is frozen once written. If we change it, we re-run the baseline too.
- Baseline and agent run against **the same simulator, the same seed, the same data**.
- Say out loud in the demo that outcomes are simulated and show the assumption table.
  Owning this is far stronger than hoping nobody asks.

Where Razorpay test-mode APIs are genuinely available (order creation, payment link
generation, retry attempt), call them for real and log real API responses. Simulate only
the customer's human response, which no test API can give us.

## Metrics to report

```
Total at risk              ₹ / count
Recovered — agent          ₹ / count / rate
Recovered — baseline       ₹ / count / rate        # do-nothing + naive-retry-all
Uplift                     ₹ and percentage points
Recovery by failure class  table
Actions taken              histogram by action type
Blocked by policy          count + reasons          ← this is a feature, show it
Escalated to human         count + ₹ value
Stopped (gave up)          count + reasons
Customer contacts sent     total, and per-customer max (proves no spam)
Cost of intervention       messages × unit cost, so uplift is net not gross
```

Report **net** recovery. Gross recovery that ignores intervention cost is the kind of
dishonest metric this hackathon is explicitly filtering for.

## Two baselines, not one

1. **Do nothing** — natural recovery only. The floor.
2. **Naive retry-all** — retry everything twice, immediately. This is what most merchants
   actually do, and it's the honest competitor. Beating *this* is the real claim; it also
   lets us show that naive retry sends messages to opted-out users and retries fraud.

## Tech

- Python 3.11, FastAPI backend, SQLite (append-only audit tables), Pydantic schemas.
- LLM used narrowly: unstructured `failure_message` → structured failure class, and
  generating customer-facing message copy (incl. Hinglish). Not for policy, not for money.
- Deterministic path must work with the LLM disabled — `--no-llm` flag. If the API is
  down during the demo, we still run.
- Frontend: whatever renders the four numbers fast. Streamlit is acceptable; do not spend
  three days on React.

## Repo layout

```
/data          generator.py, seeds, generated batches
/core          schemas.py, taxonomy.py
/diagnose      classifier.py, recoverability.py
/policy        engine.py, rules.py, limits.py      ← highest-value tests live here
/plan          planner.py
/execute       razorpay_adapter.py, channels.py, idempotency.py
/simulate      outcome.py, SIMULATION_ASSUMPTIONS.md
/audit         ledger.py, store.py
/report        metrics.py, baselines.py
/app           dashboard
/tests
run_batch.py   single entrypoint: python run_batch.py --n 500 --mode agent|baseline|naive
```

## Build order (do not reorder)

1. Schema + generator + **baseline runner**. Get a number on the board day one.
2. Taxonomy + rules-based diagnosis. No ML yet.
3. Policy engine + tests. This is where the marks are.
4. Planner + executor + idempotency.
5. Simulator (assumptions written first).
6. Metrics + dashboard.
7. LLM layer on top of the working rules system — as an upgrade, never a dependency.
8. Failure drills + demo script.

If we run out of time, we cut the LLM layer and ship a rules-based agent with excellent
measurement. That still clears the bar. A half-working LLM agent with no measured
recovery does not.

## Failure scenarios to drill and demo

Pick one to show live: **retry cap exceeded** and **₹50k human-approval gate** are the
two clearest.

- retry limit exceeded → STOP, logged
- amount over approval threshold → ESCALATE_HUMAN, no auto-action
- opted-out customer → all outbound suppressed
- fraud-flagged txn → hard block, and show naive-retry-all *would* have retried it
- executor API error → retry with backoff, then quarantine, batch continues
- duplicate run → idempotency key prevents double-charge
- LLM returns garbage / unparseable → fall back to rules, log degradation

## Working agreements for Claude Code

- Ask before adding a dependency or inventing a new field on `Transaction`.
- No new action types outside the enum without updating taxonomy + tests together.
- Every policy rule ships with its test in the same commit.
- Never write a metric function that can read the training split.
- Do not silently change simulation constants. They are a spec, not a tuning knob.
- Prefer boring, inspectable code. A judge may ask to see the policy engine on screen.

## Open decisions

- [ ] Team size and split (ML / backend / frontend / demo) — fill in.
- [ ] Razorpay test-mode keys obtained? Which endpoints are actually exercisable?
- [ ] Which vertical to anchor the story: D2C subscriptions, or B2B receivables?
      Pick one. A demo that does both does neither.
- [ ] Hinglish messaging — in scope or cut? (Nice differentiator, cheap to add at step 7.)

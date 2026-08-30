# CLAUDE.md — Revenue Recovery Agent (Razorpay Hackathon, Track 03)

## What we are building

An agent that takes a batch of failed / at-risk payment records, diagnoses **why** each
one failed, chooses a **bounded** recovery intervention, executes it, and reports **how
much money came back** against a do-nothing baseline.

The deliverable is a **product with measured outcomes**, not a model and not a chatbot.
If a design choice trades model sophistication for a cleaner measured result, take the
measured result.

**Deadline: 5 September 2026.** All eight build-order steps are complete; the project is
in the test-harden-and-rehearse phase. See "Status" below.

## Status

| | |
|---|---|
| Build order | steps 1–8 complete |
| Tests | 250 passing |
| Policy rules | 12, each with a test proving it fires |
| Failure drills | 12, runnable via `--mode drills` |
| Entry points | `README.md` (judges), `DEMO.md` (run sheet), this file (spec) |

## The single sentence the demo has to earn

> "Across 500 at-risk transactions our agent recovers **40% of at-risk value** against a
> **14% do-nothing floor**, averaged over 20 independent seeds. The naive retry-all every
> merchant already uses recovers 34% — and commits 5,463 policy violations doing it. We
> commit zero. Every decision is logged, capped, and reversible."

The original draft of this line quoted a single seed. **Never do that again** — see
"Report distributions, not runs" below. The baseline comparison is still the winning
slide; build the baseline first, before any intelligence.

### Measured result (20 seeds × 500 transactions, held-out test split)

| | recovered (value) | violations |
|---|---|---|
| Do nothing | 14.4% | — |
| Naive retry-all, gross | 33.9% | 5,463 |
| Naive retry-all, compliant only | 18.4% | — |
| **Agent** | **40.1%** | **0** |

Beats the floor **20/20** seeds, naive-compliant **20/20**, naive-gross **15/20**.
The five losing seeds are printed by `--mode sweep`, on purpose.

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

One pipeline with two **independent fallbacks**. The LLM and the Razorpay adapter sit at
different stages and degrade separately — neither branches the flow, and neither touches
policy or money.

```
                 batch of failed payments (seeded, synthetic)
                                 │
   ┌─────────────────────────────▼─────────────────────────────┐
   │ 1. DIAGNOSE                                               │
   │    rules classifier (failure_code + message)              │
   │        │                                                  │
   │        └─ confidence <= 0.55 or unclassified?             │
   │              └──> LLM (claude-opus-5) ──┐                 │
   │                   x fail/refuse/no key ─┴──> back to rules│  <- fallback 1
   └─────────────────────────────┬─────────────────────────────┘
                                 v
   2. SCORE          recoverability (how likely)
                     priority (how valuable — amount + LTV)
                                 v
   3. ORDER          highest-value first  <- so the capped human
                                             budget is spent well
                                 v
   4. PLAN           ladder rung -> action + timing (closed enum)
                                 │
                                 v
   ╔═════════════════════════════════════════════════════════╗
   ║ 5. POLICY ENGINE — 12 rules                             ║
   ║    ALLOW │ DEFER │ DOWNGRADE │ VETO                     ║
   ║    returns a PolicyDecision. No LLM here, ever.         ║
   ╚═════════════════════════════╤═══════════════════════════╝
                                 │  (executor accepts only this)
                                 v
   ┌─────────────────────────────────────────────────────────┐
   │ 6. EXECUTE   idempotency key -> replay = no-op          │
   │    Razorpay adapter: keys present ──> test-mode call    │
   │                      no keys ───────> offline, labelled │  <- fallback 2
   └─────────────────────────────┬───────────────────────────┘
                                 v
   7. SIMULATE       nested draw: recovers naturally?
                     else draw against the action's lift
                                 │
        ┌────────────────────────┴────────────────────────┐
        v                                                 v
   recovered                              not recovered -> next rung
        │                                                 (max 2)
        └────────────────┬────────────────────────────────┘
                         v
   8. AUDIT      append-only SQLite — decision, execution, outcome
                         v
   9. REPORT     vs do-nothing floor + naive competitor, 20 seeds
```

**The policy engine sits between the AI and the money. That ordering is the whole point
of the track.** The planner proposes; the policy engine disposes. Never let an LLM call
the executor directly.

Enforced structurally: `Executor.execute` accepts a `PolicyDecision`, never a
`PlannedAction`, and re-checks the verdict on arrival. There is no code path from
planning to money that skips the gate.

Two properties of the fallbacks that must not regress:

- **Both are silent and total.** `--no-llm` and a missing Razorpay key produce *the same
  numbers* as a fully-credentialed run. The LLM only sharpens diagnosis on the ~4% of
  rows the rules found ambiguous; the adapter only changes whether a call is labelled
  `test` or `offline`. If either ever moves the headline, something has leaked into the
  decision path that should not be there.
- **Step 7's draw is nested**, not independent — natural recovery first, then the action's
  lift. This is what stops an agent that declines to act from scoring below the
  do-nothing floor. See "Draw mechanics" below.

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
| `CHECKOUT_ABANDONED` | Medium | `NUDGE_*` then `SEND_PAYMENT_LINK` | T+1h, T+24h | Cart still valid check |
| `DO_NOT_HONOUR` | Low | one `RETRY`, then the ladder ends | T+24h | Ambiguous issuer response |
| `SUSPECTED_FRAUD` / stolen card | **Never** | `STOP` + flag | — | Hard block. Retrying is a compliance incident |
| `INVALID_ACCOUNT` | Never | `STOP` | — | Terminal |
| `B2B_INVOICE_OVERDUE` | — | — | — | **Out of scope** (D2C vertical). Row kept so the taxonomy is complete; the generator emits none. |

Two drafting corrections, resolved in code:
- The original table said `SINGLE_RETRY` for `DO_NOT_HONOUR`, which is not in the action
  enum. The enum is authoritative: one `RETRY`, then the ladder ends.
- `NUDGE` / `PAYMENT_LINK` are not enum members either; the real names are
  `NUDGE_WHATSAPP` / `NUDGE_EMAIL` (chosen by `channel_prefs`) and `SEND_PAYMENT_LINK`.

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
MAX_HUMAN_ESCALATIONS_PER_RUN = 20       # ~5h of one operator; human time is finite too
RECOVERY_WINDOW               = 72h      # after this, STOP
QUIET_HOURS                   = 21:00–09:00 IST  # no outbound contact, deferred not cancelled
HUMAN_APPROVAL_THRESHOLD      = ₹50,000  # above this, ESCALATE_HUMAN, do not auto-act
NEVER_RETRY                   = {SUSPECTED_FRAUD, INVALID_ACCOUNT} + chargeback_open flag
UNDIAGNOSED                   = ESCALATE_HUMAN; never act on a class we could not infer
OPT_OUT                       = absolute, checked before every outbound action
IDEMPOTENCY                   = every executed action carries a key; replay is a no-op
```

Every guardrail has a test that proves it fires. `--mode drills` demonstrates all twelve
live against the real batch.

Two additions to the original list, both closing real holes:

- **`MAX_HUMAN_ESCALATIONS_PER_RUN`.** We capped retries and messages but not human cost.
  The agent escalated without limit — ₹2,250–3,300 of operator time per 500-row batch,
  unbounded at scale. A guardrails project that bounds machine actions but not people has
  a hole in it. Because the cap binds, the runner works the queue in **descending
  escalation priority** so capacity goes to the most valuable transactions; at 2,000
  transactions the served rows average ₹7,574 against ₹1,490 turned away.
- **`UNDIAGNOSED`.** The classifier deliberately returns `None` rather than guessing. A
  test caught that nothing stopped the engine acting on that non-answer. Honouring the
  refusal is the other half of the bargain.

`CHARGEBACK_OPEN` is a transaction *state*, not a failure class, so it lives on
`Transaction.chargeback_open` and gets its own rule rather than sitting in the
`NEVER_RETRY` class set.

## Data

Synthetic, generated by us, **seeded** (`SEED=42` by default) so runs are reproducible.

- 500–2000 transactions. Start at 500; scale only once the pipeline is stable.
- Realistic failure-class distribution — insufficient funds and issuer downtime
  dominate; fraud is rare (~2%). A uniform distribution looks fake to judges.
- Include awkward cases on purpose: repeat offenders, opted-out customers, a ₹1.2L
  transaction, a customer with 4 failures in a week, one already-chargebacked record.
- **Held-out split.** 40% test, assigned by hash of `txn_id` so growing the batch never
  reshuffles which rows are test. Tune on train, report on test only.

Fields: `txn_id, customer_id, merchant_id, amount, currency, timestamp, method,
failure_code, failure_message, retry_count, customer_ltv, prior_success_rate,
prior_failures_7d, is_subscription, mandate_status, opted_out, channel_prefs,
salary_day, chargeback_open`

Two fields added beyond the original list, each because a guardrail was otherwise
unimplementable:
- **`salary_day`** (nullable) — `RETRY_SALARY_ALIGNED` cannot compute a payday window
  without it. ~60% populated; `None` forces the documented T+72h fallback, which is a
  demo beat in itself (graceful degradation on missing data).
- **`chargeback_open`** — see above.

`customer_ltv` feeds **escalation priority**, not recoverability. A ₹24,000-LTV
customer's failure is not more *likely* to recover, only worth more; mixing value into
the recoverability score would leave it meaning neither thing cleanly.

Four generator properties that exist for measurement reasons, not realism reasons:
- **Annual price tier** (₹5,999–₹11,999, 10% of rows). Without a mid-value tier the
  planted ₹50k+ rows were 39% of at-risk value and the headline swung ~18 points on a
  single draw.
- **Stale share** (15% aged past the recovery window). Non-zero so the recovery-window
  stopping rule visibly fires. The first version spread failures over 7 days against a
  72h window, which made 60% of the batch unactionable and crippled the agent.
- **Undecidable signals** (6%) — a bare `05` returned for both a balance decline and a
  generic refusal. Without them the batch was perfectly separable by the same rules we
  wrote to classify it and diagnosis scored 100%, which means we graded ourselves.
- **Unrecognised codes** (2%) — the population the LLM layer must beat, and the one that
  exercises the `UNDIAGNOSED` guardrail.

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
- A test parses `SIMULATION_ASSUMPTIONS.md` and fails if any documented probability
  disagrees with the code. Change both together or neither.

### Draw mechanics — three invariants, each learned by breaking one

1. **Draws are keyed by `txn_id`, never a sequential stream.** Every mode sees identical
   luck for the same transaction, and reordering, filtering, or parallelising the batch
   cannot change any outcome. This is what makes the priority-ordered queue safe.
2. **The draw is nested, not independent.** Ask whether the transaction recovers
   naturally; only if it does not, draw against the action's lift. An independent draw
   against the composed probability is right on average but lets a naturally-recovering
   row come out *unrecovered* once the agent acts — the agent scored below the
   do-nothing floor on seed 5 — and hands any mode a fresh roll of the natural-recovery
   dice for every action it takes, so acting more multiplies your luck.
3. **Declining to act is not the same as losing the money.** A transaction the agent
   refuses to touch still recovers at its natural rate. Without this the agent scores
   below the floor purely by exercising restraint.

Where Razorpay test-mode APIs are genuinely available (order creation, payment link
generation, retry attempt), call them for real and log real API responses. Simulate only
the customer's human response, which no test API can give us. Every adapter response is
labelled `test` or `offline` so the two are never confused.

## Metrics to report

```
Total at risk              ₹ / count
Recovered — agent          ₹ / count / rate
Recovered — baselines      ₹ / count / rate        # do-nothing + naive-retry-all
Recovered — compliant only ₹ / rate                # naive re-scored under our own rules
Uplift                     ₹ and percentage points
Multi-seed distribution    mean / stdev / min / max, and the seeds where we lose
Recovery by failure class  table
Actions taken              histogram by action type
Blocked by policy          count + reasons          ← this is a feature, show it
Escalated to human         count + ₹ value + budget used / refused
Stopped (gave up)          count + reasons
Customer contacts sent     total, and per-customer max (proves no spam)
Cost of intervention       messages × unit cost + human review time
Counterfactual recoveries  would have recovered anyway; excluded from attribution
```

Report **net** recovery. Gross recovery that ignores intervention cost is the kind of
dishonest metric this hackathon is explicitly filtering for. Human review is priced at
₹150 (≈15 min of an operator) — pretending escalation is free would make the ₹50k gate
look costless.

### Report distributions, not runs

A single-seed headline is an anecdote. The first draft of our demo quoted seed 42 at
58.6% vs 35.6%; across 30 seeds the agent and naive were statistically indistinguishable.
Always report the multi-seed aggregate, and **print the seeds where we lose** — it is the
most persuasive thing in the deck. `tests/test_robustness.py` enforces this, including a
test that fails if the agent ever wins on *every* seed, so the deck cannot drift into
overclaiming.

## Two baselines, not one — and two ways to score the second

1. **Do nothing** — natural recovery only. The floor. The one comparison that must never
   be close.
2. **Naive retry-all** — retry everything twice and message everyone, immediately. What
   most merchants actually do, and the honest competitor.

Naive is scored **twice**: gross, and compliant-only. The compliant column re-scores it by
running **the real policy engine in shadow mode** — same rules, consulted and then
ignored — so recovery obtained by retrying fraud, auto-charging above the approval
threshold, or messaging opted-out customers is separated out. Reusing the engine rather
than re-listing the rules means the two can never drift apart.

Show **both**. About a third of what the compliant column excludes is genuinely
indefensible; the rest comes from our own retry cap and recovery window, which are
business policy choices we should be transparent about. Deleting the gross number would
be the dishonest move.

## Tech

- Python 3.12 (spec said 3.11; 3.12 is what's installed and nothing depends on the
  difference). Pydantic schemas, SQLite append-only audit tables.
- **No FastAPI.** The original plan called for it; nothing in the deliverable needs an
  HTTP API. `run_batch.py` is the single entrypoint and Streamlit reads the same
  functions directly. Adding a web layer would have been three days of nothing.
- LLM used narrowly (`claude-opus-5`): unstructured `failure_message` → structured
  failure class, and customer-facing message copy including Hinglish. Not for policy,
  not for money. Consulted only where the rules were weak; degrades to rules on any
  failure; circuit breaker after 3 consecutive failures.
- Deterministic path must work with the LLM disabled — `--no-llm`. If the API is down
  during the demo, we still run, and the numbers are identical.
- Frontend: Streamlit. It computes nothing itself — a dashboard doing its own arithmetic
  is a second implementation waiting to disagree with the first.

## Repo layout

```
core/          schemas.py, taxonomy.py            ← closed Action enum lives here
data/          generator.py                        (generated batches are gitignored)
diagnose/      classifier.py, recoverability.py, llm_classifier.py
plan/          planner.py, priority.py
policy/        engine.py, rules.py, limits.py, state.py   ← highest-value tests
execute/       executor.py, razorpay_adapter.py, channels.py, idempotency.py, copy.py
simulate/      natural.py, outcome.py, SIMULATION_ASSUMPTIONS.md
audit/         store.py
report/        metrics.py, baselines.py, diagnosis.py, sweep.py
app/           dashboard.py
tests/         250 tests
runner.py      run_agent / run_naive — the two modes side by side, deliberately
drills.py      12 guardrail demonstrations
run_batch.py   single entrypoint
README.md      for judges browsing GitHub
DEMO.md        the run sheet
```

```
python run_batch.py --mode baseline|naive|agent|diagnose|drills|sweep
                    [--n 500] [--seed 42] [--split test|train|all]
                    [--compare] [--audit TXN_ID] [--seeds 20] [--no-llm]
                    [--save PATH]
```

## Build order (do not reorder) — complete

1. ✅ Schema + generator + **baseline runner**. Get a number on the board day one.
2. ✅ Taxonomy + rules-based diagnosis. No ML.
3. ✅ Policy engine + tests. This is where the marks are.
4. ✅ Planner + executor + idempotency + audit store.
5. ✅ Simulator (assumptions written first).
6. ✅ Metrics + dashboard.
7. ✅ LLM layer on top of the working rules system — an upgrade, never a dependency.
8. ✅ Failure drills + demo script.

If we run out of time, we cut the LLM layer and ship a rules-based agent with excellent
measurement. That still clears the bar. A half-working LLM agent with no measured
recovery does not.

## Failure scenarios to drill and demo

All twelve run live via `python run_batch.py --mode drills`, each printing evidence from
the real batch. Pick two to narrate: **₹50k human-approval gate** and **human review
budget** are the clearest.

- retry limit exceeded → STOP, logged
- amount over approval threshold → ESCALATE_HUMAN, no auto-action
- human review budget exhausted → STOP, and the cap bit the cheapest rows
- opted-out customer → all outbound suppressed
- fraud-flagged txn → hard block, and naive-retry-all *would* have retried it
- open chargeback → hard block regardless of diagnosis
- 72h recovery window closed → STOP
- anti-spam cap → max 3 contacts per customer vs naive's 6
- executor API error → retry with backoff, then quarantine, batch continues
- duplicate run → idempotency key prevents double-charge, and costs nothing
- LLM outage → falls back to rules, circuit breaker, logged degradation
- undiagnosed row → escalated to a human, never guessed

## Working agreements for Claude Code

- Ask before adding a dependency or inventing a new field on `Transaction`.
- No new action types outside the enum without updating taxonomy + tests together.
- Every policy rule ships with its test in the same commit.
- Never write a metric function that can read the training split.
- Do not silently change simulation constants. They are a spec, not a tuning knob.
- Prefer boring, inspectable code. A judge may ask to see the policy engine on screen.
- **Never quote a single-seed number.** Run the sweep. If a result only holds on one
  seed, it is not a result.
- **A test that measures nothing is worse than no test.** Two drills once "passed" while
  exercising nothing, because they picked transactions the policy engine independently
  vetoed. Assert that the thing under test actually happened.
- **When a number moves, find out why before believing it.** Every headline change in
  this project traced to a bug, not to an improvement.
- **Separate "how likely" from "how valuable."** Recoverability is a probability;
  priority is an expected value. Folding one into the other corrupts both.

## Open decisions

- [ ] Team size and split (ML / backend / frontend / demo) — fill in.
- [ ] Razorpay test-mode keys obtained? Which endpoints are actually exercisable?
      The adapter is written and labels responses `test` vs `offline`; it has never run
      against a live key. This is the largest untested surface in the repo.
- [ ] `ANTHROPIC_API_KEY` for the LLM layer. Every LLM test uses fakes, so degradation is
      proven but classification *quality* is not. Expect to tune the prompt on first real
      run.
- [x] **Vertical: D2C subscriptions.** Decided 21 Aug. Failure mix is
      insufficient-funds/issuer-down dominant; amounts ₹200–₹5,000 plus an annual tier,
      with planted ₹50k+ outliers to fire the approval gate. Hero action is
      `RETRY_SALARY_ALIGNED`; hero guardrails are the retry cap and the approval gate.
      B2B receivables out of scope.
- [x] **Hinglish messaging: in scope.** Built in `execute/copy.py` behind the same
      template fallback as English. Generated copy must pass a mechanical fact check
      (link and amount survive, no coercive or invented terms) before it is sent.

## Known limitations — say these before a judge finds them

- **Value-weighted recovery is high-variance.** ~10-point stdev on a 200-row test split
  because a few large transactions dominate. Mitigated by reporting 20 seeds; the real
  fix is a larger test split or a count-weighted primary metric.
- **Salary alignment is constrained by our own window.** The 72h recovery window and real
  payday alignment conflict for most customers. The planner aligns when payday falls
  inside the window and otherwise takes the T+72h fallback. Widening the window for that
  class is a policy change and belongs here, not in a quiet edit to the planner.
- **The LLM layer is unproven against the live API.** See open decisions.
- **Outcomes are simulated.** Stated everywhere, including on the dashboard.

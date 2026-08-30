# Revenue Recovery Agent

An agent that takes a batch of failed payments, diagnoses **why** each one
failed, chooses a **bounded** recovery action, executes it, and reports **how
much money came back** against a do-nothing baseline.

Built for the **Razorpay AI Buildathon 2026**, Track 03.

> **All data is synthetic and all recovery outcomes are simulated** from a
> frozen, published assumption table ([`simulate/SIMULATION_ASSUMPTIONS.md`](simulate/SIMULATION_ASSUMPTIONS.md)).
> No real payment or customer data is used. Not affiliated with Razorpay.

---

## The result

Averaged over **20 independent seeds**, 500 transactions each, reported on a
held-out test split:

| | recovered (value) | policy violations |
|---|---|---|
| Do nothing | 14.4% | — |
| Naive retry-all, gross | 33.9% | 5,463 |
| Naive retry-all, compliant only | 18.4% | — |
| **Agent** | **40.1%** | **0** |

- Beats the do-nothing floor on **20/20** seeds
- Beats naive's compliant recovery on **20/20** seeds
- Beats naive's *gross* recovery on **15/20** seeds — not a clean sweep, and
  `--mode sweep` prints the seeds where we lose

Reproduce it:

```bash
python run_batch.py --mode sweep --seeds 20
```

### Why two columns for naive

Naive retry-all recovers 33.9% gross — but a large share of that is taken by
retrying suspected fraud, auto-charging a ₹1.2L transaction with no human
approval, and messaging customers who opted out. That is not revenue you keep;
it is a liability. The "compliant only" column re-scores naive by running
**the real policy engine in shadow mode** — same rules, ignored rather than
obeyed — so there is no second copy of the rules to drift. Gross is still
reported, because deleting it would be the dishonest move.

---

## Architecture

One pipeline with two **independent fallbacks** — the LLM and the Razorpay
adapter sit at different stages and degrade separately. Neither branches the
flow, and neither touches policy or money.

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

Three things in that diagram are load-bearing:

- **Step 5 is the only authoriser.** `Executor.execute` accepts a
  `PolicyDecision`, never a `PlannedAction`, and re-checks the verdict on
  arrival — so there is no code path from planning to money that skips the gate.
  No LLM is consulted inside the engine.
- **Both fallbacks are silent and total.** `--no-llm` and a missing Razorpay key
  produce *the same numbers* as a fully-credentialed run. The LLM only sharpens
  diagnosis on the ~4% of rows the rules found ambiguous; the adapter only
  changes whether a call is labelled `test` or `offline`.
- **Step 7's draw is nested**, not independent — natural recovery first, then the
  action's lift. That is why an agent which declines to act can never score below
  the do-nothing floor.

---

## Guardrails

Every one is hard-coded in [`policy/`](policy/) and has a test proving it fires.

| Guardrail | Limit |
|---|---|
| Retries per transaction | 2, counting prior history |
| Messages per transaction | 2 |
| Messages per customer | 3 per 7 days, **across transactions** |
| Recovery window | 72h, then STOP |
| Quiet hours | 21:00–09:00 IST, deferred not cancelled |
| Human approval threshold | ₹50,000 — escalate, never auto-act |
| Human review budget | 20 reviewers per run, spent highest-value-first |
| Never retry | suspected fraud, invalid account, open chargeback |
| Opt-out | absolute, checked before every outbound action |
| Undiagnosed | escalated to a person, never guessed |
| Idempotency | replay is a no-op and costs nothing |

Run them as live demonstrations against the real batch:

```bash
python run_batch.py --mode drills      # 12 drills, each with evidence
```

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run_batch.py --mode baseline --split test   # the do-nothing floor
python run_batch.py --mode naive    --split test   # the honest competitor
python run_batch.py --mode agent    --split test --compare
python run_batch.py --mode drills                  # guardrails, live
python run_batch.py --mode diagnose --split test   # classifier accuracy
python run_batch.py --mode sweep    --seeds 20     # robustness
streamlit run app/dashboard.py                     # dashboard
```

Everything runs **offline**. No API key, no network, no Razorpay credentials.

---

## Diagnosis

Rules-first, LLM as an upgrade. On the held-out split:

```
Accuracy 94.0%   Coverage 96.5%

0.90+  (unambiguous code)   100.0%
0.80+  (ambiguous, resolved) 100.0%
0.50+  (ambiguous, defaulted) 61.5%
0.00   (unclassified)          0.0%
```

Accuracy falls monotonically as confidence falls, so the confidence number is
worth something to the planner. It is 94% and not 100% **on purpose** — the
generator emits genuinely undecidable gateway signals (a bare `05` is returned
for both a balance decline and a generic refusal). A rules layer scoring 100%
against data we generated ourselves would mean we had graded ourselves.

The 3.5% it cannot classify are escalated to a human, never guessed.

### The LLM layer

Claude (`claude-opus-5`) is consulted **only** for rows the rules found
ambiguous or could not classify, with the response constrained to the failure
enum via structured outputs. It degrades to rules on everything — missing
credentials, network error, refusal, unparseable output — and a circuit breaker
disables it after three consecutive failures. `--no-llm` skips it entirely.

The LLM never touches policy, never proposes an action, and never reaches the
executor.

---

## Honest limitations

- **Outcomes are simulated.** The assumption table was written before the agent
  existed and is frozen; a test parses it and fails if code and document
  disagree. All modes run against the same simulator, seed, and transactions.
- **Value-weighted recovery is high-variance.** A 200-row test split has a
  ~10-point standard deviation because a few large transactions dominate. That
  is why we report 20 seeds rather than one.
- **Salary-aligned retry is constrained.** The 72h recovery window and real
  payday alignment conflict for most customers; the planner aligns only when
  payday falls inside the window and otherwise uses the T+72h fallback.
- **The Razorpay adapter runs offline by default** and labels every response
  `test` or `offline` so the two are never confused.

---

## Repository layout

```
core/       schemas and taxonomy — the closed Action enum lives here
data/       seeded synthetic generator with planted edge cases
diagnose/   rules classifier, recoverability scoring, LLM upgrade
plan/       intervention ladder and escalation priority
policy/     the guardrails — highest-value tests in the repo
execute/    executor, idempotency, channels, Razorpay adapter
simulate/   frozen outcome model + SIMULATION_ASSUMPTIONS.md
audit/      append-only decision store
report/     metrics, baselines, diagnosis eval, multi-seed sweep
app/        Streamlit dashboard
drills.py   guardrail demonstrations
run_batch.py single entrypoint
```

## Testing

```bash
python -m pytest tests/ -q      # 249 tests
```

The load-bearing ones: [`tests/test_policy.py`](tests/test_policy.py) proves
every guardrail fires; [`tests/test_robustness.py`](tests/test_robustness.py)
runs the comparison across 20 seeds so a single-seed result cannot reach a
slide; and one test parses `SIMULATION_ASSUMPTIONS.md` and fails if a
probability is nudged without updating the document.

See [`DEMO.md`](DEMO.md) for the demo script and the questions we expect.

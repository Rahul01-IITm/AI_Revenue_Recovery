# Demo script — Revenue Recovery Agent

**Setup before you present:** `source .venv/bin/activate`. Everything below runs
offline. No API key, no network, no Razorpay credentials required — the LLM
layer and the Razorpay adapter both degrade cleanly and say so.

Total runtime: about 4 minutes of commands, plus talking.

---

## 0. The one sentence (15s)

> "Across 500 at-risk transactions worth ₹9.7L, our agent recovered 58.6% of the
> value. Doing nothing recovers 10%. The industry-standard naive retry recovers
> 35% — and commits 83 policy violations doing it. We commit zero. Every
> decision is logged, capped, and reversible."

Then prove each clause.

---

## 1. The floor, the competitor, and us (60s)

```bash
python run_batch.py --mode baseline --split test
python run_batch.py --mode naive    --split test
python run_batch.py --mode agent    --split test --compare
```

| | Recovered (value) | Violations |
|---|---|---|
| Do nothing | 10.0% | — |
| Naive retry-all | 35.6% | **83** |
| **Agent** | **58.6%** | **0** |

**Say out loud, before anyone asks:**

> "Outcomes are simulated. The assumption table is in
> `simulate/SIMULATION_ASSUMPTIONS.md`, it was written before the agent existed,
> and it's frozen — there's a test that fails if the code and the document
> disagree. All three modes run against the same simulator, the same seed, and
> the same transactions."

**The detail worth pausing on:** naive recovers *slightly more by count* (77 vs
76) and far less by value. It gets lucky on cheap rows while burning both its
retries on high-value ones that a retry cannot fix — an expired card is still
expired on the second attempt.

---

## 2. The policy engine (60s) — the heart of the pitch

Put `policy/rules.py` on screen. Eleven pure functions, evaluated in order.

> "The planner proposes. This engine disposes. The executor takes a
> `PolicyDecision`, never a `PlannedAction` — so there is no code path from the
> AI to money that skips this file. No LLM is consulted here, and none ever
> should be."

Then run the guardrails rather than describing them:

```bash
python run_batch.py --mode drills
```

Eleven drills, all passing, each with evidence from the real batch. **Pick two
to read aloud:**

- **₹50k approval gate** — `txn_000007` is ₹120,000 → DOWNGRADE to
  `ESCALATE_HUMAN`. The money is still pursued, by someone accountable.
- **Fraud** — `txn_000005` → VETO. And the same line reports that naive
  retry-all *would have retried fraud 16 times*. That's not a lost sale, it's a
  compliance incident.

---

## 3. Stopping rules and the anti-spam cap (30s)

From the agent run's output:

```
  Blocked by policy (a feature, not a bug):
    recovery_window, retry_cap, no_channel, opt_out, message_cap_customer_7d

  Stopped (gave up), by reason:
    planner:not_worth_chasing, recovery_window, retry_cap, ...

  Customer contacts    max 3 per customer   <- proves no spam
```

> "The agent gives up, and logs why it gave up. Naive's max is 6 messages to one
> customer; ours is 3, because the cap is cross-transaction — a customer with
> four failed subscriptions does not get four dunning sequences."

---

## 4. Audit trail (30s)

```bash
python run_batch.py --mode agent --audit txn_000007
```

Every decision carries an id, a timestamp, an input snapshot, the rule that
fired, and the reason.

> "The store is append-only — enforced by SQLite triggers, not by convention.
> UPDATE and DELETE abort at the database. If a judge asks how we know nobody
> edited the log, the answer is that the database refuses the statement."

---

## 5. Diagnosis quality (30s)

```bash
python run_batch.py --mode diagnose --split test
```

```
  Accuracy 94.0%   Coverage 96.5%

  0.90+  (unambiguous code)   100.0%
  0.80+  (ambiguous, resolved) 100.0%
  0.50+  (ambiguous, defaulted) 61.5%
  0.00   (unclassified)          0.0%
```

> "Accuracy falls monotonically as confidence falls, so the confidence number
> is worth something to the planner. And it's 94%, not 100% — the batch contains
> genuinely undecidable gateway signals on purpose. A rules layer scoring 100%
> against data we generated ourselves would mean we'd graded ourselves."

The 3.5% it can't classify are escalated to a human, never guessed.

---

## 6. Failure drill, live (30s)

Kill the LLM and re-run:

```bash
python run_batch.py --mode agent --no-llm --split test
```

Identical numbers.

> "The LLM only upgrades diagnosis on rows the rules found ambiguous. If it's
> down mid-demo — or the API key expires — a circuit breaker trips after three
> failures and the batch finishes rules-only. That's not a fallback we hope
> works; it's `drills.py`, drill 10, and it's in the test suite."

---

## 7. Close (15s)

```bash
python -m pytest tests/ -q
```

> "225 tests. The highest-value ones are in `tests/test_policy.py` — every
> guardrail in the spec has a test proving it actually fires. One test parses
> the simulation assumptions document and fails if the code and the document
> disagree, so nobody can quietly nudge a probability to make a slide look
> better."

---

## Questions we expect, and the honest answers

**"Are these numbers real?"**
The recovery outcomes are simulated from a frozen, documented assumption table.
The pipeline, policy engine, audit store, and idempotency are real code. Where
Razorpay test-mode endpoints are available the adapter calls them and labels the
response `test` vs `offline` so the two are never confused.

**"Did you tune the simulator so you'd win?"**
Column A (do-nothing) was frozen before the agent existed. Column B was written
without touching Column A. Both are parsed by tests that fail on divergence.
The predicted count-weighted floor was ~13% and the measured floor came out
~15% — we didn't back-fit it to the number in our own pitch.

**"Why is the agent barely ahead of naive on count?"**
It isn't ahead on count — naive wins by one. We win on *value* because we don't
waste retries on failures a retry can't fix. That's the whole thesis.

**"What's the weakest part?"**
Salary-aligned retry. The 72h recovery window and real payday alignment conflict
for most customers, so the planner aligns only when payday genuinely falls
inside the window and otherwise takes the T+72h fallback. Widening the window
for that class is a policy change and belongs in the spec, not a quiet edit.

**"What would you build next?"**
Per-customer recovery-window policy, and a real Razorpay test-mode run to
replace the offline adapter path.

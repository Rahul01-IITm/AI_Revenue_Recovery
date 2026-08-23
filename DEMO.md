# Demo script — Revenue Recovery Agent

**Setup:** `source .venv/bin/activate`. Everything runs offline — no API key, no
network, no Razorpay credentials. The LLM layer and the Razorpay adapter both
degrade cleanly and say so.

About 5 minutes of commands, plus talking.

> **Numbers below are the 20-seed aggregate**, not a single run. Seed 42 is
> reproducible and shown for the live commands, but never quote a single seed as
> the headline — see §1.

---

## 0. The one sentence (15s)

> "Across 500 at-risk transactions, our agent recovers **42% of at-risk value**
> against a **14% do-nothing floor** — averaged over 20 independent seeds, not
> one lucky run. The naive retry-all every merchant already uses recovers 34%,
> and racks up 5,463 policy violations doing it. We commit zero. Every decision
> is logged, capped, and reversible."

Then prove each clause.

---

## 1. Robustness first (60s) — lead with this

Most demos show one run. Show the distribution instead:

```bash
python run_batch.py --mode sweep --seeds 20
```

```
  mode                        mean   stdev     min     max
  do-nothing floor           14.4%   11.1%    1.9%   41.4%
  naive, gross               33.9%   11.9%   15.3%   57.3%
  naive, compliant only      18.4%    5.7%   11.5%   34.6%
  agent                      42.2%   13.2%   17.8%   65.6%

  agent vs do-nothing              +27.8%   ahead on 20/20 seeds
  agent vs naive (gross)            +8.4%   ahead on 15/20 seeds
  agent vs naive (compliant)       +23.8%   ahead on 19/20 seeds

  violations   agent 0   naive 5463

  Seeds where the agent loses to naive on gross recovery: 4, 5, 11, 15, 20
```

**Say the last line out loud.** It is the most persuasive thing in the demo:

> "We print the seeds where we lose. On raw recovery we're ahead on 15 of 20 —
> good, not a clean sweep. The claim that *is* a clean sweep is against the
> do-nothing floor: 20 out of 20."

**The two ways to score naive, and why we show both:**

> "Gross credits naive for every rupee, including money it took by retrying
> suspected fraud, auto-charging a ₹1.2L transaction with no human approval, and
> messaging customers who opted out. That's not revenue you keep — it's a
> liability. Strip only those and naive is at 18.4%. We show both columns because
> deleting the gross number would be the dishonest move."

The compliant column is computed by running **the real policy engine** over
naive in shadow mode — same rules, ignored rather than obeyed — so there is no
second copy of the rules to drift.

---

## 2. A single reproducible run (45s)

```bash
python run_batch.py --mode baseline --split test
python run_batch.py --mode naive    --split test
python run_batch.py --mode agent    --split test --compare
```

Seed 42: floor **10.0%**, naive gross **31.4%**, agent **57.6%**.

**Say out loud, before anyone asks:**

> "Outcomes are simulated. The assumption table is in
> `simulate/SIMULATION_ASSUMPTIONS.md`, written before the agent existed, and
> frozen — a test parses it and fails if the code and the document disagree. All
> three modes run against the same simulator, the same seed, and the same
> transactions."

---

## 3. The policy engine (60s) — the heart of the pitch

Put `policy/rules.py` on screen. Eleven pure functions, evaluated in order.

> "The planner proposes. This engine disposes. The executor takes a
> `PolicyDecision`, never a `PlannedAction` — there is no code path from the AI
> to money that skips this file. No LLM is consulted here, and none ever should
> be."

Then run the guardrails rather than describing them:

```bash
python run_batch.py --mode drills
```

Eleven drills, each with evidence from the real batch. **Read two aloud:**

- **₹50k approval gate** — `txn_000007` is ₹120,000 → DOWNGRADE to
  `ESCALATE_HUMAN`. The money is still pursued, by someone accountable.
- **Fraud** — `txn_000005` → VETO, and the same line reports that naive
  *would have retried fraud 16 times*. Not a lost sale; a compliance incident.

---

## 4. Stopping rules and the anti-spam cap (30s)

From the agent run:

```
  Blocked by policy (a feature, not a bug):
    recovery_window, retry_cap, no_channel, opt_out, message_cap_customer_7d

  Stopped (gave up), by reason:
    planner:not_worth_chasing, recovery_window, retry_cap, ...

  Customer contacts    max 3 per customer   <- proves no spam
```

> "The agent gives up, and logs why. The cap is cross-transaction — a customer
> with four failed subscriptions does not get four dunning sequences."

---

## 5. Audit trail (30s)

```bash
python run_batch.py --mode agent --no-llm --audit txn_000007
```

> "Every decision carries an id, a timestamp, an input snapshot, the rule that
> fired, and the reason. The store is append-only — enforced by SQLite triggers,
> not convention. UPDATE and DELETE abort at the database."

---

## 6. Diagnosis quality (30s)

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

> "Accuracy falls monotonically as confidence falls, so the confidence number is
> worth something to the planner. And it's 94%, not 100% — the batch contains
> genuinely undecidable gateway signals on purpose. A rules layer scoring 100%
> against data we generated ourselves would mean we'd graded ourselves."

The 3.5% it can't classify are escalated to a human, never guessed.

---

## 7. Failure drill, live (30s)

```bash
python run_batch.py --mode agent --no-llm --split test
```

Identical numbers.

> "The LLM only upgrades diagnosis on rows the rules found ambiguous. If it's
> down mid-demo, a circuit breaker trips after three failures and the batch
> finishes rules-only. That's `drills.py`, drill 10, and it's in the test suite."

---

## 8. Close (20s)

```bash
python -m pytest tests/ -q
```

> "236 tests. The highest-value ones are in `tests/test_policy.py` — every
> guardrail has a test proving it fires. One parses the simulation assumptions
> document and fails if the code and the document disagree. And
> `tests/test_robustness.py` runs the whole comparison across 20 seeds, so a
> result that only works on one seed fails the build instead of reaching a slide."

---

## Questions we expect, and the honest answers

**"Did you cherry-pick the seed?"**
Run `--mode sweep`. It prints the seeds where we lose. On gross recovery we win
15 of 20; against the do-nothing floor, 20 of 20. There's a test that fails if
the agent ever wins on *every* seed, so the deck can't quietly start
overclaiming.

**"Isn't 'compliant recovery' just moving the goalposts?"**
Fair challenge, so we show gross too. The split is: about a third of what we
exclude is genuinely indefensible (retrying fraud, auto-charging above the
approval threshold, messaging opted-out customers), and the rest comes from our
own retry cap and 72h window, which are business policy choices we're
transparent about. Even excluding *only* the indefensible actions, we're ahead.

**"Are these numbers real?"**
The recovery outcomes are simulated from a frozen, documented assumption table.
The pipeline, policy engine, audit store, and idempotency are real code. Where
Razorpay test-mode endpoints are available the adapter calls them and labels
the response `test` vs `offline`.

**"Did you tune the simulator so you'd win?"**
Column A was frozen before the agent existed; Column B was written without
touching it; tests parse both and fail on divergence. We also found and fixed a
bug that had been making our own numbers *worse* — see below.

**"What did testing actually find?"**
A real simulator bug. The outcome draw was independent of the natural-recovery
draw, so a transaction that would have recovered on its own could come out
unrecovered once the agent acted — the agent scored *below* the do-nothing floor
on seed 5, and any mode got extra rolls of the natural-recovery dice just for
acting more. The draw is now nested. The change log in
`SIMULATION_ASSUMPTIONS.md` records it, and no probability changed.

**"What's the weakest part?"**
Variance. Value-weighted recovery on a 200-row test split has a 13-point
standard deviation, because a handful of high-value rows dominate. That's why we
report 20 seeds. Second weakest: salary-aligned retry, where the 72h window and
real payday alignment conflict for most customers.

**"What would you build next?"**
A per-class recovery window (the 72h cap is what suppresses salary alignment),
and a real Razorpay test-mode run to replace the offline adapter path.

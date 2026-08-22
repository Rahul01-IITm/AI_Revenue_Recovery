# Simulation assumptions

Outcomes in this project are **simulated**. This document is the spec for that
simulation. It is written before the code that consumes it and frozen once
written. Say this out loud in the demo and put this table on screen — owning it
is far stronger than hoping nobody asks.

**Status: Column A (do-nothing) is FROZEN as of 22 Aug 2026.**
Columns B+ (per-action probabilities) land in build step 5 and must be written
without editing Column A. If Column A ever changes, every baseline number in
every deck is invalid and must be regenerated.

---

## Column A — natural recovery, no intervention

`P(recovery | failure_class, action = NONE)` within the 72h recovery window.

This is the do-nothing baseline: the floor that the agent must beat. It models
the customer fixing the problem unprompted — retrying the card themselves,
topping up the account, completing the checkout they abandoned.

| Failure class | P(natural recovery) | Reasoning |
|---|---:|---|
| `ISSUER_DOWN` | **0.30** | Nothing is wrong with the customer or the instrument. Outages resolve within hours and a motivated subscriber re-attempts. Highest natural rate in the table. |
| `3DS_TIMEOUT` | **0.18** | Intent was clearly present — the customer reached the auth step. A meaningful share re-attempt without prompting, but the drop-off at 3DS is real friction. |
| `INSUFFICIENT_FUNDS` | **0.12** | Depends on the balance recovering inside 72h. For salaried customers most of the month, it does not. This is deliberately low — it is the single largest class, so an inflated value here would flatter the baseline and understate our uplift. |
| `CHECKOUT_ABANDONED` | **0.10** | Weak intent by definition. Some return via their own browser history; most do not. |
| `CARD_EXPIRED` | **0.05** | Requires the customer to notice and proactively update an instrument. Rare without a prompt — this is precisely why the class needs `REQUEST_INSTRUMENT_UPDATE`. |
| `DO_NOT_HONOUR` | **0.05** | Ambiguous issuer response. A minority are transient risk-engine declines that clear on a later attempt. |
| `MANDATE_EXPIRED` | **0.04** | Customer is usually unaware the mandate lapsed. Near-zero unprompted action. |
| `MANDATE_REVOKED` | **0.01** | The customer revoked it deliberately. Natural "recovery" here is essentially an accident. |
| `SUSPECTED_FRAUD` | **0.00** | Hard zero. Not a recovery target under any circumstance. |
| `INVALID_ACCOUNT` | **0.00** | Hard zero. Terminal — the account does not exist. |
| `B2B_INVOICE_OVERDUE` | **0.00** | Out of scope for the D2C vertical; no such transactions are generated. |

### What this column implies

Against the frozen D2C failure mix, the count-weighted natural recovery rate is
approximately **13%**.

That number was **not** tuned to match the illustrative "11%" in CLAUDE.md's
demo sentence. Each cell was set from the reasoning in the table above and the
aggregate is whatever falls out. If we later back-fit this column to make a
slide read better, the entire result becomes circular and a sharp judge will
find it in one question.

Amount-weighted recovery will differ from count-weighted, because the planted
high-value outliers carry their own failure classes. Both are reported.

---

## Draw mechanics

Three properties matter more than the exact probabilities:

**1. Outcomes are paired across modes.** The natural-recovery draw for a given
transaction is seeded from `(batch_seed, txn_id, "natural")` — not from a
sequential RNG. The same transaction therefore gets the same draw in the
do-nothing run, the naive-retry run, and the agent run.

This means the three modes are compared on *identical* luck. A difference in
results is attributable to the policy, not to sampling noise. Without pairing,
a 500-row batch would carry enough variance to move the headline number by
several points between modes, and the comparison would be worthless.

**2. Draw order is irrelevant.** Because each draw is keyed by `txn_id` rather
than drawn from a shared stream, filtering to the test split, reordering the
batch, or parallelising the run cannot change any outcome.

**3. Natural recovery is independent of agent action.** In step 5, an
intervention's success probability composes with this column rather than
replacing it — a retry on an `ISSUER_DOWN` transaction that would have
recovered naturally must not be double-counted as recovered *because of* the
agent. The uplift metric subtracts the counterfactual.

---

## Change log

| Date | Change | Consequence |
|---|---|---|
| 2026-08-22 | Column A written and frozen. | Baseline is now reproducible. |

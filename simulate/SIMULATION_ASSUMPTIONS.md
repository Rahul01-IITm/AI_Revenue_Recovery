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

## Column B — incremental lift from an intervention

`P(recovery | failure_class, action)` is **not** stated directly. Interventions
are modelled as *lift on top of* Column A:

```
P(recover | action) = P_natural + (1 - P_natural) x LIFT[class][action]
```

Composing rather than replacing matters. If an action's probability replaced the
natural rate, a retry on an `ISSUER_DOWN` transaction that was going to recover
anyway would be counted as recovered *because of* the agent. It wasn't. The
`counterfactual` flag records those cases and uplift subtracts them, so the
agent is never credited for luck.

**Status: Column B FROZEN as of 23 Aug 2026.** Column A was frozen first and was
not touched while writing this.

| Failure class | Action | LIFT | Reasoning |
|---|---|---:|---|
| `ISSUER_DOWN` | `RETRY` | **0.55** | The instrument and the customer are both fine; only the issuer was down. Highest lift in the table. |
| `ISSUER_DOWN` | `ESCALATE_HUMAN` | **0.40** | A person can re-run it, but adds no insight a retry lacks. |
| `INSUFFICIENT_FUNDS` | `RETRY_SALARY_ALIGNED` | **0.45** | Timed for when money is actually in the account. This is the agent's headline action. |
| `INSUFFICIENT_FUNDS` | `RETRY` | **0.08** | An immediate retry hits the same empty account. Near-worthless, and it costs issuer trust — this is what naive-retry-all does. |
| `INSUFFICIENT_FUNDS` | `NUDGE_WHATSAPP` | **0.15** | Prompting the customer to top up works sometimes. |
| `INSUFFICIENT_FUNDS` | `NUDGE_EMAIL` | **0.10** | Same mechanism, weaker channel. |
| `INSUFFICIENT_FUNDS` | `ESCALATE_HUMAN` | **0.30** | A person can negotiate timing. |
| `CARD_EXPIRED` | `REQUEST_INSTRUMENT_UPDATE` | **0.35** | The only action that can work: the customer must supply a new card. |
| `CARD_EXPIRED` | `SEND_PAYMENT_LINK` | **0.25** | Also gets a fresh instrument, with more friction. |
| `CARD_EXPIRED` | `RETRY` | **0.00** | Hard zero. The card is expired; a retry is guaranteed to fail and is pure waste. |
| `CARD_EXPIRED` | `ESCALATE_HUMAN` | **0.30** | A person can call and take new details. |
| `MANDATE_EXPIRED` | `REQUEST_MANDATE_RENEWAL` | **0.30** | Customer is usually unaware it lapsed; asking works. |
| `MANDATE_EXPIRED` | `SEND_PAYMENT_LINK` | **0.20** | Recovers this cycle but not the subscription. |
| `MANDATE_EXPIRED` | `RETRY` | **0.00** | Hard zero. No valid mandate to debit against. |
| `MANDATE_EXPIRED` | `ESCALATE_HUMAN` | **0.25** | |
| `MANDATE_REVOKED` | `REQUEST_MANDATE_RENEWAL` | **0.12** | Deliberate cancellation. Asking rarely reverses a considered decision. |
| `MANDATE_REVOKED` | `SEND_PAYMENT_LINK` | **0.15** | One-off payment is an easier ask than reinstating authority. |
| `MANDATE_REVOKED` | `RETRY` | **0.00** | Hard zero, and arguably a compliance problem. |
| `MANDATE_REVOKED` | `ESCALATE_HUMAN` | **0.20** | |
| `3DS_TIMEOUT` | `SEND_PAYMENT_LINK` | **0.50** | Intent was demonstrated at the auth step; a link removes the friction that lost it. |
| `3DS_TIMEOUT` | `NUDGE_WHATSAPP` | **0.25** | Reminder without removing the friction. |
| `3DS_TIMEOUT` | `NUDGE_EMAIL` | **0.18** | |
| `3DS_TIMEOUT` | `RETRY` | **0.12** | Re-triggers the same auth the customer already abandoned. |
| `3DS_TIMEOUT` | `ESCALATE_HUMAN` | **0.35** | |
| `CHECKOUT_ABANDONED` | `SEND_PAYMENT_LINK` | **0.30** | |
| `CHECKOUT_ABANDONED` | `NUDGE_WHATSAPP` | **0.25** | |
| `CHECKOUT_ABANDONED` | `NUDGE_EMAIL` | **0.15** | |
| `CHECKOUT_ABANDONED` | `RETRY` | **0.02** | There is no authorised payment to retry. |
| `CHECKOUT_ABANDONED` | `ESCALATE_HUMAN` | **0.25** | |
| `DO_NOT_HONOUR` | `RETRY` | **0.15** | A minority are transient risk-engine declines. |
| `DO_NOT_HONOUR` | `SEND_PAYMENT_LINK` | **0.18** | A different instrument may pass. |
| `DO_NOT_HONOUR` | `ESCALATE_HUMAN` | **0.20** | |
| `SUSPECTED_FRAUD` | *any* | **0.00** | Hard zero. Not a recovery target. |
| `INVALID_ACCOUNT` | *any* | **0.00** | Hard zero. Terminal. |

Any `(class, action)` pair absent from this table has lift **0.00**. Silence is
not permission: an action nobody reasoned about earns nothing.

### Timing multipliers

Lift is scaled by how well-timed the action is. Same action, wrong moment,
less money.

| Condition | Multiplier | Reasoning |
|---|---:|---|
| `ISSUER_DOWN` + `RETRY` within 1h of failure | **0.40** | The outage probably has not cleared. Retrying instantly is what naive-retry-all does and it wastes most of the opportunity. |
| `3DS_TIMEOUT` + link later than 2h | **0.60** | A warm lead cools fast. |
| `CHECKOUT_ABANDONED` + action later than 4h | **0.70** | Cart intent decays. |
| everything else | **1.00** | |

### Cost of intervention

Recovery is reported **net**. Gross recovery that ignores what it cost to earn
is the dishonest metric this hackathon is filtering for.

| Item | Cost | Note |
|---|---:|---|
| WhatsApp message | Rs.0.35 | Utility template rate. |
| SMS | Rs.0.18 | |
| Email | Rs.0.02 | |
| Human review | Rs.150.00 | ~15 minutes of an operations person. Escalation is not free, and pretending otherwise would make the Rs.50k gate look costless. |
| Retry attempt | Rs.0.00 | No marginal gateway fee on a failed attempt. |

---

## Change log

| Date | Change | Consequence |
|---|---|---|
| 2026-08-22 | Column A written and frozen. | Baseline is now reproducible. |
| 2026-08-23 | Column B, timing multipliers, and costs written and frozen. Column A untouched. | Agent and naive runs are now reproducible against a fixed floor. |

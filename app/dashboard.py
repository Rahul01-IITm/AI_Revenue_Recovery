"""Dashboard. Renders the four numbers that matter, fast.

    streamlit run app/dashboard.py

Deliberately thin. Every number here is computed by `runner` and `report`, and
nothing is calculated in this file — a dashboard that does its own arithmetic
is a second implementation waiting to disagree with the first.
"""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from data.generator import DEFAULT_SEED, generate_batch
from report.baselines import do_nothing
from report.diagnosis import evaluate
from report.metrics import rupees
from runner import run_agent, run_naive

st.set_page_config(page_title="Revenue Recovery Agent", layout="wide")


@st.cache_data(show_spinner=False)
def _run(n: int, seed: int, split: str):
    batch = generate_batch(n=n, seed=seed)
    s = None if split == "all" else split
    agent, store = run_agent(batch, split=s)
    return (
        do_nothing(batch, split=s),
        run_naive(batch, split=s),
        agent,
        evaluate(batch, split=s),
        store.reconstruct("txn_000007"),
        dict(store.blocked_counts("agent")),
    )


st.title("Revenue Recovery Agent")
st.caption(
    "Outcomes are **simulated** from a frozen assumptions table "
    "(`simulate/SIMULATION_ASSUMPTIONS.md`). Baseline, naive, and agent run "
    "against the same simulator, the same seed, and the same transactions."
)

with st.sidebar:
    st.header("Run")
    n = st.slider("Batch size", 100, 2000, 500, step=100)
    seed = st.number_input("Seed", value=DEFAULT_SEED, step=1)
    split = st.radio(
        "Split", ["test", "train", "all"], index=0,
        help="Report on test. Numbers from train are tuned-against and not quotable.",
    )
    if split == "train":
        st.warning("Train split: do not put these numbers in the deck.")

baseline, naive, agent, diag, audit_text, blocked = _run(n, int(seed), split)

# --- The headline ------------------------------------------------------------

st.subheader("Recovered against the do-nothing floor")

c1, c2, c3, c4 = st.columns(4)
c1.metric("At risk", rupees(agent.at_risk), f"{agent.count} transactions")
c2.metric(
    "Do nothing", rupees(baseline.net_recovered),
    f"{baseline.recovery_rate_amount:.1%}", delta_color="off",
)
c3.metric(
    "Naive retry-all", rupees(naive.net_recovered),
    f"{naive.recovery_rate_amount:.1%}", delta_color="off",
)
c4.metric(
    "Agent (net)", rupees(agent.net_recovered),
    f"{agent.recovery_rate_amount - baseline.recovery_rate_amount:+.1%} vs floor",
)

st.caption(
    f"Net of Rs.{agent.intervention_cost:,.0f} intervention cost. "
    f"{agent.counterfactual_recovered} recoveries would have happened anyway "
    "and are excluded from attribution."
)

# --- Compliance --------------------------------------------------------------

st.subheader("Compliance: the agent against the honest competitor")

left, right = st.columns(2)
with left:
    st.markdown("**Agent**")
    st.metric("Policy violations", sum(agent.violations.values()) or 0)
    st.metric("Max contacts per customer", agent.max_contacts_per_customer)
    st.metric(
        "Escalated to a human",
        f"{agent.escalated_count} ({rupees(agent.escalated_value)})",
    )
with right:
    st.markdown("**Naive retry-all**")
    st.metric("Policy violations", sum(naive.violations.values()))
    st.metric("Max contacts per customer", naive.max_contacts_per_customer)
    if naive.violations:
        st.dataframe(
            [{"violation": k, "count": v} for k, v in
             sorted(naive.violations.items(), key=lambda kv: -kv[1])],
            hide_index=True, use_container_width=True,
        )

# --- Funnel ------------------------------------------------------------------

st.subheader("What the agent did")

fa, fb, fc = st.columns(3)
with fa:
    st.markdown("**Actions taken**")
    st.dataframe(
        [{"action": k, "n": v} for k, v in
         sorted(agent.actions_taken.items(), key=lambda kv: -kv[1])],
        hide_index=True, use_container_width=True,
    )
with fb:
    st.markdown("**Blocked by policy**")
    st.caption("A feature, not a bug.")
    st.dataframe(
        [{"rule": k, "n": v} for k, v in
         sorted(agent.blocked_by_policy.items(), key=lambda kv: -kv[1])],
        hide_index=True, use_container_width=True,
    )
with fc:
    st.markdown("**Stopped, by reason**")
    st.dataframe(
        [{"reason": k, "n": v} for k, v in
         sorted(agent.stopped_reasons.items(), key=lambda kv: -kv[1])],
        hide_index=True, use_container_width=True,
    )

# --- Per class ---------------------------------------------------------------

st.subheader("Recovery by failure class")

rows = []
base_by_class = {b.failure_class: b for b in baseline.by_class}
for b in agent.by_class:
    floor = base_by_class.get(b.failure_class)
    rows.append(
        {
            "failure class": b.failure_class.value,
            "n": b.count,
            "at risk": rupees(b.at_risk),
            "do nothing": f"{floor.rate:.0%}" if floor else "-",
            "agent": f"{b.rate:.0%}",
            "recovered": rupees(b.recovered_amount),
        }
    )
st.dataframe(rows, hide_index=True, use_container_width=True)

# --- Diagnosis ---------------------------------------------------------------

st.subheader("Diagnosis quality")

d1, d2, d3 = st.columns(3)
d1.metric("Accuracy", f"{diag.accuracy:.1%}", "unclassified counted as wrong")
d2.metric("Coverage", f"{diag.coverage:.1%}")
d3.metric("Undiagnosed", diag.total - diag.classified, "escalated, never guessed")

st.markdown("**Calibration** — accuracy should fall as confidence falls.")
st.dataframe(
    [
        {"confidence band": band, "correct": ok, "n": total,
         "accuracy": f"{ok / total:.1%}" if total else "-"}
        for band, (ok, total) in sorted(diag.accuracy_by_confidence.items())
    ],
    hide_index=True, use_container_width=True,
)

# --- Audit trail -------------------------------------------------------------

st.subheader("Audit trail")
st.caption(
    "Every decision has an id, a timestamp, an input snapshot, a rule, and a "
    "reason. The store is append-only: UPDATE and DELETE abort at the database."
)
st.code(audit_text or "no decisions recorded for this transaction", language="text")

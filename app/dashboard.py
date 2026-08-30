"""Demo dashboard.

    streamlit run app/dashboard.py

Deliberately thin: every number is computed by `runner`, `report`, and
`drills` — nothing is calculated here. A dashboard doing its own arithmetic is
a second implementation waiting to disagree with the first.

Chart conventions (validated palette, light surface):
  agent = blue, the emphasis colour        naive = orange
  do-nothing floor = muted gray            violations = status red / green
Bars are direct-labelled so identity never rests on colour alone.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

# `streamlit run app/dashboard.py` puts app/ on sys.path, not the repo root, so
# the project imports below fail with ModuleNotFoundError. Prepend the root
# before importing anything from it. Note this is invisible to
# `streamlit.testing.v1.AppTest`, which runs from the repo root and therefore
# passes either way — a screenshot is what caught it.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from data.generator import DEFAULT_SEED, generate_batch  # noqa: E402
from report.baselines import do_nothing  # noqa: E402
from report.diagnosis import evaluate  # noqa: E402
from report.metrics import rupees  # noqa: E402
from report.sweep import sweep  # noqa: E402
from runner import run_agent, run_naive  # noqa: E402

# --- Design tokens -----------------------------------------------------------

AGENT = "#2a78d6"      # categorical slot 1
NAIVE = "#eb6834"      # categorical slot 2
FLOOR = "#8a8a85"      # de-emphasis gray
GOOD = "#0ca30c"       # status: good
CRITICAL = "#d03b3b"   # status: critical
INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#e8e7e3"

st.set_page_config(
    page_title="Revenue Recovery Agent",
    page_icon="📉",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .block-container { padding-top: 2.2rem; max-width: 1400px; }
      [data-testid="stMetricValue"] { font-size: 2.0rem; font-weight: 600; }
      [data-testid="stMetricLabel"] { font-size: .8rem; color: #52514e; }
      h1 { font-size: 2.1rem !important; letter-spacing: -.02em; }
      h2 { font-size: 1.25rem !important; margin-top: .4rem; letter-spacing: -.01em; }
      h3 { font-size: 1.0rem !important; color: #52514e; font-weight: 600; }
      .caption { color:#52514e; font-size:.86rem; line-height:1.5; }
      .pill { display:inline-block; padding:.18rem .6rem; border-radius:999px;
              font-size:.74rem; font-weight:600; letter-spacing:.02em; }
      .pill-ok   { background:#e7f6e7; color:#0a7a0a; }
      .pill-bad  { background:#fbeaea; color:#a72c2c; }
      .pill-note { background:#eef3fb; color:#1c5cab; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _axis(title: str = "", fmt: str | None = None, **kw):
    """Recessive axis styling. `format` is omitted when unset — Altair 6 rejects
    an explicit None rather than treating it as 'use the default'."""
    if fmt is not None:
        kw["format"] = fmt
    return alt.Axis(
        title=title, labelColor=INK_MUTED, titleColor=INK_MUTED,
        gridColor=GRID, domainColor=GRID, tickColor=GRID, labelFontSize=11,
        titleFontSize=11, **kw,
    )


def _chart(c: alt.Chart, height: int) -> alt.Chart:
    return c.properties(height=height).configure_view(
        strokeWidth=0
    ).configure_axis(labelFont="sans-serif")


# --- Data --------------------------------------------------------------------


@st.cache_data(show_spinner="Running the batch…")
def _single(n: int, seed: int, split: str):
    batch = generate_batch(n=n, seed=seed)
    s = None if split == "all" else split
    agent, store = run_agent(batch, split=s)
    return (
        do_nothing(batch, split=s),
        run_naive(batch, split=s),
        agent,
        evaluate(batch, split=s),
        store.reconstruct("txn_000007"),
        {t.txn_id: float(t.amount) for t in batch.transactions},
    )


@st.cache_data(show_spinner="Running the multi-seed sweep…")
def _sweep(n: int, seeds: int):
    return sweep(n=n, seeds=seeds)


@st.cache_data(show_spinner="Running the guardrail drills…")
def _drills(n: int, seed: int):
    import drills

    return [
        (d.name, d.passed, d.evidence, d.why_it_matters)
        for d in drills.run_all(generate_batch(n=n, seed=seed))
    ]


# --- Sidebar -----------------------------------------------------------------

with st.sidebar:
    st.markdown("### Run configuration")
    n = st.select_slider("Batch size", [200, 500, 1000, 2000], value=500)
    seed = st.number_input("Seed", value=DEFAULT_SEED, step=1)
    split = st.radio(
        "Split", ["test", "train", "all"], index=0,
        help="Report on test. Numbers from train are tuned-against and not quotable.",
    )
    seeds = st.slider("Seeds in the sweep", 5, 30, 20)
    if split == "train":
        st.warning("Train split — do not put these numbers in the deck.")
    st.divider()
    st.markdown(
        '<div class="caption">Outcomes are <b>simulated</b> from a frozen '
        "assumption table written before the agent existed "
        "(<code>simulate/SIMULATION_ASSUMPTIONS.md</code>). All modes run "
        "against the same simulator, seed, and transactions.</div>",
        unsafe_allow_html=True,
    )

floor, naive, agent, diag, audit_text, amounts = _single(n, int(seed), split)

# --- Header ------------------------------------------------------------------

st.title("Revenue Recovery Agent")
st.markdown(
    '<div class="caption">Diagnoses why each payment failed, chooses a '
    "<b>bounded</b> recovery action, executes it, and reports how much money came "
    "back against a do-nothing baseline. Every decision is logged, capped, and "
    "reversible.</div>",
    unsafe_allow_html=True,
)
st.write("")

k1, k2, k3, k4 = st.columns(4)
k1.metric("At risk", rupees(agent.at_risk), f"{agent.count} transactions")
k2.metric(
    "Recovered — agent", rupees(agent.net_recovered),
    f"{agent.recovery_rate_amount:.1%} of value",
)
k3.metric(
    "vs do-nothing floor",
    f"+{(agent.recovery_rate_amount - floor.recovery_rate_amount) * 100:.1f} pts",
    f"floor is {floor.recovery_rate_amount:.1%}",
)
k4.metric(
    "Policy violations", f"{sum(agent.violations.values())}",
    f"naive commits {sum(naive.violations.values())}", delta_color="off",
)

st.divider()

# --- 1. Robustness -----------------------------------------------------------

st.header("Is this one lucky run?")
st.markdown(
    '<div class="caption">A single-seed headline is an anecdote. Every seed in '
    "the sweep is plotted — including the ones where the agent loses.</div>",
    unsafe_allow_html=True,
)

rows = _sweep(n, seeds)
sweep_df = pd.DataFrame(
    [
        {"seed": r.seed, "mode": mode, "rate": val}
        for r in rows
        for mode, val in (
            ("Agent", r.agent),
            ("Naive (gross)", r.naive_gross),
            ("Do nothing", r.floor),
        )
    ]
)
means = sweep_df.groupby("mode", as_index=False)["rate"].mean()
order = ["Agent", "Naive (gross)", "Do nothing"]
scale = alt.Scale(domain=order, range=[AGENT, NAIVE, FLOOR])

points = (
    alt.Chart(sweep_df)
    .mark_circle(size=90, opacity=0.75, stroke="white", strokeWidth=1.5)
    .encode(
        x=alt.X("seed:O", axis=_axis("seed")),
        y=alt.Y("rate:Q", axis=_axis("recovered, % of at-risk value", ".0%")),
        color=alt.Color(
            "mode:N", scale=scale,
            legend=alt.Legend(title=None, orient="top", labelFontSize=12),
            sort=order,
        ),
        tooltip=[
            alt.Tooltip("seed:O"),
            alt.Tooltip("mode:N", title="mode"),
            alt.Tooltip("rate:Q", format=".1%", title="recovered"),
        ],
    )
)
mean_rules = (
    alt.Chart(means)
    .mark_rule(strokeDash=[5, 4], size=1.5, opacity=0.85)
    .encode(y="rate:Q", color=alt.Color("mode:N", scale=scale, legend=None, sort=order))
)
st.altair_chart(_chart(points + mean_rules, 330), width="stretch")

a = [r.agent for r in rows]
ng = [r.naive_gross for r in rows]
nc = [r.naive_compliant for r in rows]
fl = [r.floor for r in rows]
lose = [r.seed for r in rows if r.agent <= r.naive_gross]

c1, c2, c3 = st.columns(3)
c1.metric(
    "Agent vs floor", f"+{statistics.mean(a) - statistics.mean(fl):.1%}",
    f"ahead on {sum(1 for r in rows if r.agent > r.floor)}/{len(rows)} seeds",
)
c2.metric(
    "Agent vs naive (gross)", f"+{statistics.mean(a) - statistics.mean(ng):.1%}",
    f"ahead on {len(rows) - len(lose)}/{len(rows)} seeds",
)
c3.metric(
    "Agent vs naive (compliant)", f"+{statistics.mean(a) - statistics.mean(nc):.1%}",
    f"ahead on {sum(1 for r in rows if r.agent > r.naive_compliant)}/{len(rows)} seeds",
)

st.markdown(
    f'<div class="caption"><span class="pill pill-note">honest</span> &nbsp; '
    f"Seeds where the agent <b>loses</b> to naive on gross recovery: "
    f"<b>{', '.join(map(str, lose)) or 'none'}</b>. Dashed lines are means. "
    f"Standard deviation on the agent is {statistics.stdev(a):.1%} — value-weighted "
    "recovery is high-variance because a few large transactions dominate, which "
    "is exactly why 20 seeds are reported instead of one.</div>",
    unsafe_allow_html=True,
)

st.divider()

# --- 2. Two ways to score the competitor -------------------------------------

st.header("Recovery against both baselines")

comp = pd.DataFrame(
    [
        {"mode": "Do nothing", "rate": floor.recovery_rate_amount,
         "value": float(floor.net_recovered), "hero": False},
        {"mode": "Naive — compliant only", "rate": naive.compliant_recovery_rate,
         "value": float(naive.compliant_recovered_amount), "hero": False},
        {"mode": "Naive — gross", "rate": naive.recovery_rate_amount,
         "value": float(naive.net_recovered), "hero": False},
        {"mode": "Agent", "rate": agent.recovery_rate_amount,
         "value": float(agent.net_recovered), "hero": True},
    ]
)
bars = (
    alt.Chart(comp)
    .mark_bar(cornerRadiusEnd=4, height=30)
    .encode(
        y=alt.Y("mode:N", sort=list(comp["mode"]), axis=_axis()),
        x=alt.X("rate:Q", axis=_axis("recovered, % of at-risk value", ".0%")),
        color=alt.condition(alt.datum.hero, alt.value(AGENT), alt.value(FLOOR)),
        tooltip=[
            alt.Tooltip("mode:N", title=""),
            alt.Tooltip("rate:Q", format=".1%", title="rate"),
            alt.Tooltip("value:Q", format=",.0f", title="Rs."),
        ],
    )
)
labels = bars.mark_text(align="left", dx=8, fontSize=12, color=INK).encode(
    text=alt.Text("rate:Q", format=".1%"), color=alt.value(INK)
)
st.altair_chart(_chart(bars + labels, 200), width="stretch")

st.markdown(
    '<div class="caption">Naive is scored twice. <b>Gross</b> credits every rupee, '
    "including money taken by retrying suspected fraud, auto-charging above the "
    "₹50,000 approval threshold, and messaging opted-out customers — a liability, "
    "not revenue. <b>Compliant only</b> re-scores it by running the <i>real policy "
    "engine in shadow mode</i>: same rules, ignored rather than obeyed, so there is "
    "no second copy to drift. Both are shown because deleting the gross number "
    "would be the dishonest move.</div>",
    unsafe_allow_html=True,
)

st.divider()

# --- 3. Compliance -----------------------------------------------------------

left, right = st.columns([1, 1.35])

with left:
    st.header("Compliance")
    st.metric("Agent violations", sum(agent.violations.values()))
    st.markdown(
        f'<span class="pill pill-ok">clean</span> &nbsp;'
        f'<span class="caption">max {agent.max_contacts_per_customer} messages per '
        f"customer across {agent.contacts_sent} sent</span>",
        unsafe_allow_html=True,
    )
    st.write("")
    st.metric("Naive violations", sum(naive.violations.values()))
    st.markdown(
        f'<span class="pill pill-bad">unguarded</span> &nbsp;'
        f'<span class="caption">max {naive.max_contacts_per_customer} messages per '
        "customer</span>",
        unsafe_allow_html=True,
    )

with right:
    st.header("What an unguarded system does")
    if naive.violations:
        v = pd.DataFrame(
            sorted(naive.violations.items(), key=lambda kv: -kv[1]),
            columns=["violation", "count"],
        )
        v["violation"] = v["violation"].str.replace("_", " ")
        vb = (
            alt.Chart(v)
            .mark_bar(cornerRadiusEnd=4, height=20, color=CRITICAL)
            .encode(
                y=alt.Y("violation:N", sort="-x", axis=_axis()),
                x=alt.X("count:Q", axis=_axis("occurrences")),
                tooltip=["violation:N", "count:Q"],
            )
        )
        vl = vb.mark_text(align="left", dx=6, fontSize=11, color=INK).encode(
            text="count:Q"
        )
        st.altair_chart(_chart(vb + vl, 230), width="stretch")

st.divider()

# --- 4. What the agent did ---------------------------------------------------

st.header("What the agent did")
f1, f2, f3 = st.columns(3)

with f1:
    st.markdown("### Actions taken")
    if agent.actions_taken:
        a_df = pd.DataFrame(
            sorted(agent.actions_taken.items(), key=lambda kv: -kv[1]),
            columns=["action", "n"],
        )
        a_df["action"] = a_df["action"].str.replace("_", " ").str.title()
        ac = (
            alt.Chart(a_df)
            .mark_bar(cornerRadiusEnd=3, height=17, color=AGENT)
            .encode(
                y=alt.Y("action:N", sort="-x", axis=_axis()),
                x=alt.X("n:Q", axis=_axis()),
                tooltip=["action:N", "n:Q"],
            )
        )
        st.altair_chart(
            _chart(ac + ac.mark_text(align="left", dx=5, fontSize=11,
                                     color=INK).encode(text="n:Q"), 250),
            width="stretch",
        )

with f2:
    st.markdown("### Blocked by policy")
    st.markdown('<div class="caption">A feature, not a bug.</div>',
                unsafe_allow_html=True)
    if agent.blocked_by_policy:
        b_df = pd.DataFrame(
            sorted(agent.blocked_by_policy.items(), key=lambda kv: -kv[1]),
            columns=["rule", "n"],
        )
        b_df["rule"] = b_df["rule"].str.replace("_", " ")
        bc = (
            alt.Chart(b_df)
            .mark_bar(cornerRadiusEnd=3, height=17, color=FLOOR)
            .encode(
                y=alt.Y("rule:N", sort="-x", axis=_axis()),
                x=alt.X("n:Q", axis=_axis()),
                tooltip=["rule:N", "n:Q"],
            )
        )
        st.altair_chart(
            _chart(bc + bc.mark_text(align="left", dx=5, fontSize=11,
                                     color=INK).encode(text="n:Q"), 250),
            width="stretch",
        )

with f3:
    st.markdown("### Stopped, by reason")
    st.markdown('<div class="caption">The agent gives up, and logs why.</div>',
                unsafe_allow_html=True)
    if agent.stopped_reasons:
        s_df = pd.DataFrame(
            sorted(agent.stopped_reasons.items(), key=lambda kv: -kv[1]),
            columns=["reason", "n"],
        )
        s_df["reason"] = s_df["reason"].str.replace("_", " ").str.replace(":", " · ")
        sc = (
            alt.Chart(s_df)
            .mark_bar(cornerRadiusEnd=3, height=17, color=FLOOR)
            .encode(
                y=alt.Y("reason:N", sort="-x", axis=_axis()),
                x=alt.X("n:Q", axis=_axis()),
                tooltip=["reason:N", "n:Q"],
            )
        )
        st.altair_chart(
            _chart(sc + sc.mark_text(align="left", dx=5, fontSize=11,
                                     color=INK).encode(text="n:Q"), 250),
            width="stretch",
        )

esc1, esc2 = st.columns(2)
esc1.metric("Escalated to a human", agent.escalated_count,
            f"worth {rupees(agent.escalated_value)}")
esc2.metric("Intervention cost", rupees(agent.intervention_cost),
            "messages + human review time", delta_color="off")

st.divider()

# --- 5. Per failure class ----------------------------------------------------

st.header("Recovery by failure class")
st.markdown(
    '<div class="caption">Distinct failure classes must produce visibly '
    "different behaviour — otherwise the agent is a retry loop with extra "
    "steps.</div>",
    unsafe_allow_html=True,
)

base_by_class = {b.failure_class: b for b in floor.by_class}
cls_df = pd.DataFrame(
    [
        {
            "class": b.failure_class.value.replace("_", " "),
            "series": series,
            "rate": rate,
            "at_risk": float(b.at_risk),
            "n": b.count,
        }
        for b in agent.by_class
        for series, rate in (
            ("Agent", b.rate),
            ("Do nothing",
             base_by_class[b.failure_class].rate
             if b.failure_class in base_by_class else 0.0),
        )
    ]
)
sort_order = [
    b.failure_class.value.replace("_", " ")
    for b in sorted(agent.by_class, key=lambda x: -x.at_risk)
]
cls_chart = (
    alt.Chart(cls_df)
    .mark_bar(cornerRadiusEnd=3, height=11)
    .encode(
        y=alt.Y("class:N", sort=sort_order, axis=_axis()),
        x=alt.X("rate:Q", axis=_axis("recovered (share of transactions)", ".0%")),
        yOffset=alt.YOffset("series:N", sort=["Agent", "Do nothing"]),
        color=alt.Color(
            "series:N",
            scale=alt.Scale(domain=["Agent", "Do nothing"], range=[AGENT, FLOOR]),
            legend=alt.Legend(title=None, orient="top"),
        ),
        tooltip=[
            alt.Tooltip("class:N", title="failure class"),
            alt.Tooltip("series:N", title=""),
            alt.Tooltip("rate:Q", format=".0%", title="recovered"),
            alt.Tooltip("n:Q", title="transactions"),
            alt.Tooltip("at_risk:Q", format=",.0f", title="Rs. at risk"),
        ],
    )
)
st.altair_chart(_chart(cls_chart, 420), width="stretch")

st.divider()

# --- 6. Diagnosis ------------------------------------------------------------

st.header("Diagnosis quality")
d1, d2, d3 = st.columns(3)
d1.metric("Accuracy", f"{diag.accuracy:.1%}", "unclassified counted as wrong",
          delta_color="off")
d2.metric("Coverage", f"{diag.coverage:.1%}",
          f"{diag.classified}/{diag.total} classified", delta_color="off")
d3.metric("Undiagnosed", diag.total - diag.classified,
          "escalated, never guessed", delta_color="off")

cal = pd.DataFrame(
    [
        {"band": band, "correct": ok, "n": total, "accuracy": ok / total if total else 0}
        for band, (ok, total) in sorted(diag.accuracy_by_confidence.items())
    ]
)
cal_bars = (
    alt.Chart(cal)
    .mark_bar(cornerRadiusEnd=4, height=26, color=AGENT)
    .encode(
        y=alt.Y("band:N", sort=list(cal["band"]), axis=_axis()),
        x=alt.X("accuracy:Q", axis=_axis("accuracy within band", ".0%"),
                scale=alt.Scale(domain=[0, 1])),
        tooltip=["band:N", "correct:Q", "n:Q",
                 alt.Tooltip("accuracy:Q", format=".1%")],
    )
)
cal_lab = cal_bars.mark_text(align="left", dx=8, fontSize=12, color=INK).encode(
    text=alt.Text("accuracy:Q", format=".0%"), color=alt.value(INK)
)
st.altair_chart(_chart(cal_bars + cal_lab, 200), width="stretch")
st.markdown(
    '<div class="caption">Accuracy falls as confidence falls, so the confidence '
    "number is worth something to the planner. It is not 100% <b>on purpose</b> — "
    "the batch contains genuinely undecidable gateway signals. A rules layer "
    "scoring 100% against data we generated ourselves would mean we graded "
    "ourselves.</div>",
    unsafe_allow_html=True,
)

st.divider()

# --- 7. Guardrails & audit ---------------------------------------------------

g1, g2 = st.columns([1.1, 1])

with g1:
    st.header("Guardrail drills")
    st.markdown(
        '<div class="caption">Run live against the real batch, not described on '
        "a slide.</div>", unsafe_allow_html=True,
    )
    if st.button("Run all drills", type="primary"):
        st.session_state["drills"] = _drills(n, int(seed))
    for name, passed, evidence, why in st.session_state.get("drills", []):
        pill = "pill-ok" if passed else "pill-bad"
        st.markdown(
            f'<span class="pill {pill}">{"PASS" if passed else "FAIL"}</span> '
            f"&nbsp;<b>{name}</b><br>"
            f'<span class="caption">{evidence}<br><i>{why}</i></span><br><br>',
            unsafe_allow_html=True,
        )

with g2:
    st.header("Audit trail")
    st.markdown(
        '<div class="caption">Every decision carries an id, a timestamp, an input '
        "snapshot, the rule that fired, and the reason. The store is append-only — "
        "UPDATE and DELETE abort at the database.</div>",
        unsafe_allow_html=True,
    )
    st.code(audit_text or "no decisions recorded for this transaction",
            language="text")

st.divider()
st.markdown(
    '<div class="caption">Outcomes are <b>simulated</b>. '
    "<code>simulate/SIMULATION_ASSUMPTIONS.md</code> is frozen and a test fails if "
    "the code and the document disagree. The pipeline, policy engine, audit store, "
    "and idempotency are real code.</div>",
    unsafe_allow_html=True,
)

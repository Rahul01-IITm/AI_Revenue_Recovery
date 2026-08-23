"""Multi-seed sweep — the answer to "did you cherry-pick the seed?".

A single-seed headline is an anecdote. This runs the whole comparison across N
seeds and reports the distribution, including the seeds where the agent loses,
because hiding those is how a demo becomes a lie.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from data.generator import generate_batch
from report.baselines import do_nothing
from report.metrics import rupees


@dataclass
class SweepRow:
    seed: int
    floor: float
    naive_gross: float
    naive_compliant: float
    agent: float
    naive_violations: int
    agent_violations: int


def sweep(n: int = 500, seeds: int = 20) -> list[SweepRow]:
    from runner import run_agent, run_naive

    rows = []
    for seed in range(1, seeds + 1):
        batch = generate_batch(n=n, seed=seed)
        f = do_nothing(batch, split="test")
        nv = run_naive(batch, split="test")
        ag, _ = run_agent(batch, split="test")
        rows.append(
            SweepRow(
                seed=seed,
                floor=f.recovery_rate_amount,
                naive_gross=nv.recovery_rate_amount,
                naive_compliant=nv.compliant_recovery_rate,
                agent=ag.recovery_rate_amount,
                naive_violations=sum(nv.violations.values()),
                agent_violations=sum(ag.violations.values()),
            )
        )
    return rows


def render(rows: list[SweepRow]) -> str:
    agent = [r.agent for r in rows]
    gross = [r.naive_gross for r in rows]
    comp = [r.naive_compliant for r in rows]
    floor = [r.floor for r in rows]

    lines = [
        "",
        f"  Multi-seed sweep -- {len(rows)} seeds, test split",
        "  " + "=" * 66,
        "",
        f"  {'mode':<24}{'mean':>8}{'stdev':>8}{'min':>8}{'max':>8}",
        "  " + "-" * 56,
    ]
    for name, vals in [
        ("do-nothing floor", floor),
        ("naive, gross", gross),
        ("naive, compliant only", comp),
        ("agent", agent),
    ]:
        lines.append(
            f"  {name:<24}{statistics.mean(vals):>8.1%}"
            f"{statistics.stdev(vals):>8.1%}{min(vals):>8.1%}{max(vals):>8.1%}"
        )

    def head_to_head(label, other):
        d = [a - o for a, o in zip(agent, other)]
        wins = sum(1 for x in d if x > 0)
        return (
            f"  agent vs {label:<22}{statistics.mean(d):>+8.1%}"
            f"   ahead on {wins}/{len(rows)} seeds"
        )

    lines += [
        "",
        head_to_head("do-nothing", floor),
        head_to_head("naive (gross)", gross),
        head_to_head("naive (compliant)", comp),
        "",
        f"  violations   agent {sum(r.agent_violations for r in rows)}"
        f"   naive {sum(r.naive_violations for r in rows)}",
        "",
        "  Seeds where the agent loses to naive on gross recovery: "
        + (
            ", ".join(str(r.seed) for r in rows if r.agent <= r.naive_gross)
            or "none"
        ),
        "",
    ]
    return "\n".join(lines)

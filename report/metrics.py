"""Run results and their presentation.

One rule enforced here rather than remembered: `RunResult.split` records which
split produced the numbers, and `render` prints it. A number in the deck that
came from `train` is therefore visible as such rather than silently wrong.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from core.schemas import Split
from core.taxonomy import FailureClass


class ClassBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    failure_class: FailureClass
    count: int
    at_risk: Decimal
    recovered_count: int
    recovered_amount: Decimal

    @property
    def rate(self) -> float:
        return self.recovered_count / self.count if self.count else 0.0


class RunResult(BaseModel):
    """Outcome of one mode over one split."""

    model_config = ConfigDict(frozen=True)

    mode: str
    split: Split | None
    seed: int

    count: int
    at_risk: Decimal
    recovered_count: int
    recovered_amount: Decimal

    by_class: list[ClassBreakdown]

    #: Cost of intervention. Zero for do-nothing; populated once the executor
    #: sends real messages, so uplift is reported net rather than gross.
    intervention_cost: Decimal = Decimal("0")

    @property
    def recovery_rate_count(self) -> float:
        return self.recovered_count / self.count if self.count else 0.0

    @property
    def recovery_rate_amount(self) -> float:
        if self.at_risk == 0:
            return 0.0
        return float(self.recovered_amount / self.at_risk)

    @property
    def net_recovered(self) -> Decimal:
        return self.recovered_amount - self.intervention_cost


def rupees(x: Decimal) -> str:
    """Indian-convention short form: Rs.4.1L, Rs.1.2Cr."""
    x = Decimal(x)
    if x >= 10_000_000:
        return f"Rs.{x / 10_000_000:.2f}Cr"
    if x >= 100_000:
        return f"Rs.{x / 100_000:.2f}L"
    return f"Rs.{x:,.0f}"


def build_breakdown(
    rows: list[tuple[FailureClass, Decimal, bool]],
) -> list[ClassBreakdown]:
    """Aggregate `(true_class, amount, recovered)` triples per failure class."""
    agg: dict[FailureClass, dict] = defaultdict(
        lambda: {"count": 0, "at_risk": Decimal("0"), "rc": 0, "ra": Decimal("0")}
    )
    for cls, amount, recovered in rows:
        a = agg[cls]
        a["count"] += 1
        a["at_risk"] += amount
        if recovered:
            a["rc"] += 1
            a["ra"] += amount

    return [
        ClassBreakdown(
            failure_class=cls,
            count=a["count"],
            at_risk=a["at_risk"],
            recovered_count=a["rc"],
            recovered_amount=a["ra"],
        )
        for cls, a in sorted(agg.items(), key=lambda kv: -kv[1]["at_risk"])
    ]


def render(result: RunResult) -> str:
    """Human-readable summary. This is what goes on screen in the demo."""
    lines = [
        "",
        f"  Mode                 {result.mode}",
        f"  Split                {result.split or 'all'}   (seed {result.seed})",
        "",
        f"  Transactions         {result.count}",
        f"  Total at risk        {rupees(result.at_risk)}",
        "",
        f"  Recovered (count)    {result.recovered_count}"
        f"  ({result.recovery_rate_count:.1%})",
        f"  Recovered (value)    {rupees(result.recovered_amount)}"
        f"  ({result.recovery_rate_amount:.1%})",
    ]

    if result.intervention_cost > 0:
        lines += [
            f"  Intervention cost    {rupees(result.intervention_cost)}",
            f"  Net recovered        {rupees(result.net_recovered)}",
        ]

    lines += ["", "  By failure class:", ""]
    header = f"    {'class':<22}{'n':>5}{'at risk':>12}{'recovered':>12}{'rate':>8}"
    lines += [header, "    " + "-" * (len(header) - 4)]
    for b in result.by_class:
        lines.append(
            f"    {b.failure_class:<22}{b.count:>5}"
            f"{rupees(b.at_risk):>12}{rupees(b.recovered_amount):>12}"
            f"{b.rate:>8.1%}"
        )
    lines.append("")
    return "\n".join(lines)


def render_comparison(baseline: RunResult, candidate: RunResult) -> str:
    """The winning slide: candidate against baseline, in value and points."""
    d_amount = candidate.net_recovered - baseline.net_recovered
    d_points = (
        candidate.recovery_rate_amount - baseline.recovery_rate_amount
    ) * 100
    return "\n".join(
        [
            "",
            f"  {baseline.mode:<20}{rupees(baseline.net_recovered):>12}"
            f"  ({baseline.recovery_rate_amount:.1%})",
            f"  {candidate.mode:<20}{rupees(candidate.net_recovered):>12}"
            f"  ({candidate.recovery_rate_amount:.1%})",
            "  " + "-" * 46,
            f"  {'uplift':<20}{rupees(d_amount):>12}  ({d_points:+.1f} pts)",
            "",
        ]
    )

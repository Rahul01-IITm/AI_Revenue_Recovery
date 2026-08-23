"""Multi-seed robustness — the guard against cherry-picking a good seed.

A single-seed headline is not a result. These tests run the comparison across
many seeds and assert the claim holds on the aggregate, so a number that only
works on seed 42 fails the suite instead of reaching the deck.

They are slower than the rest of the suite by design; correctness of the
headline claim is worth a few seconds.
"""

from __future__ import annotations

import statistics

import pytest

from data.generator import generate_batch
from report.baselines import do_nothing
from runner import run_agent, run_naive

SEEDS = range(1, 21)


@pytest.fixture(scope="module")
def sweep():
    rows = []
    for seed in SEEDS:
        batch = generate_batch(n=500, seed=seed)
        agent, _ = run_agent(batch, split="test")
        naive = run_naive(batch, split="test")
        floor = do_nothing(batch, split="test")
        rows.append((floor, naive, agent))
    return rows


def test_agent_beats_the_do_nothing_floor_on_every_seed(sweep):
    """The floor is the one comparison that must never be close."""
    for floor, _, agent in sweep:
        assert agent.recovery_rate_amount > floor.recovery_rate_amount


def test_agent_beats_naive_compliant_recovery_on_the_aggregate(sweep):
    """The headline claim, asserted across seeds rather than on one lucky draw."""
    diffs = [
        agent.recovery_rate_amount - naive.compliant_recovery_rate
        for _, naive, agent in sweep
    ]
    assert statistics.mean(diffs) > 0.10, f"mean uplift only {statistics.mean(diffs):.1%}"
    wins = sum(1 for d in diffs if d > 0)
    assert wins >= len(diffs) * 0.85, f"agent ahead in only {wins}/{len(diffs)} seeds"


def test_agent_never_commits_a_violation_on_any_seed(sweep):
    """Zero is the whole compliance claim. One violation on one seed breaks it."""
    for _, _, agent in sweep:
        assert agent.violations == {}
        assert agent.forbidden_recovered_count == 0


def test_naive_always_commits_violations(sweep):
    """If naive ever came out clean, the contrast would be a coincidence."""
    for _, naive, _ in sweep:
        assert sum(naive.violations.values()) > 0


def test_agent_never_exceeds_the_customer_contact_cap(sweep):
    for _, _, agent in sweep:
        assert agent.max_contacts_per_customer <= 3


def test_agent_beats_naive_gross_recovery_but_not_on_every_seed(sweep):
    """The gross claim, with its real strength — and its real limits.

    The agent leads on gross recovery on the aggregate, but not on every seed.
    Both halves are asserted: the lead must hold, and the demo must not claim a
    clean sweep it does not have. A test that only checked the lead would let
    the deck drift into overclaiming.
    """
    diffs = [
        agent.recovery_rate_amount - naive.recovery_rate_amount
        for _, naive, agent in sweep
    ]
    mean = statistics.mean(diffs)
    wins = sum(1 for d in diffs if d > 0)

    assert mean > 0.04, f"gross lead collapsed to {mean:+.1%}"
    assert wins >= len(diffs) * 0.65, f"agent ahead on gross in only {wins}/{len(diffs)}"
    assert wins < len(diffs), (
        "agent now wins on gross on EVERY seed — DEMO.md says it does not. "
        "Update the deck rather than leaving it understated."
    )


def test_an_intervention_can_never_destroy_recovery(sweep):
    """The bug this suite was written to catch.

    An acting mode must recover a superset of what would have come back on its
    own. When the outcome draw was independent of the natural draw, the agent
    scored *below* the do-nothing floor on seed 5 — money the transaction would
    have returned unprompted vanished because the agent acted.
    """
    for floor, naive, agent in sweep:
        assert agent.recovered_amount >= floor.recovered_amount
        assert naive.recovered_amount >= floor.recovered_amount
        assert agent.recovered_count >= floor.recovered_count
        assert naive.recovered_count >= floor.recovered_count


def test_compliant_recovery_never_exceeds_gross(sweep):
    for _, naive, agent in sweep:
        assert naive.compliant_recovered_amount <= naive.recovered_amount
        assert agent.compliant_recovered_amount <= agent.recovered_amount


def test_the_result_is_not_an_artefact_of_one_batch_size():
    """A claim that only holds at n=500 is a claim about n=500."""
    for n in (300, 500, 900):
        batch = generate_batch(n=n, seed=42)
        agent, _ = run_agent(batch, split="test")
        naive = run_naive(batch, split="test")
        assert agent.recovery_rate_amount > naive.compliant_recovery_rate, n


def test_agent_run_is_deterministic():
    """Same batch, same result. Twice."""
    batch = generate_batch(n=300, seed=42)
    first, _ = run_agent(batch, split="test")
    second, _ = run_agent(batch, split="test")
    assert first.model_dump() == second.model_dump()

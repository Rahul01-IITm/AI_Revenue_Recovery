"""Every failure drill must pass. A regression here breaks a guardrail."""

from __future__ import annotations

import pytest

import drills
from data.generator import generate_batch


@pytest.fixture(scope="module")
def batch():
    return generate_batch(n=500, seed=42)


@pytest.mark.parametrize("drill", drills.ALL_DRILLS, ids=lambda d: d.__name__)
def test_drill_passes(drill, batch):
    result = drill(batch)
    assert result.passed, f"{result.name}: {result.evidence}"


def test_every_drill_explains_why_it_matters(batch):
    """A drill nobody can explain is a demo prop, not a guardrail."""
    for result in drills.run_all(batch):
        assert result.why_it_matters
        assert result.evidence


def test_drill_set_covers_the_claude_md_scenarios():
    """The scenarios CLAUDE.md names must each have a drill."""
    names = {d.__name__ for d in drills.ALL_DRILLS}
    for required in (
        "drill_retry_cap",
        "drill_approval_threshold",
        "drill_opt_out",
        "drill_fraud_hard_block",
        "drill_executor_quarantine",
        "drill_idempotency",
        "drill_llm_degradation",
    ):
        assert required in names

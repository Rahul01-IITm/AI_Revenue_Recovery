#!/usr/bin/env python3
"""Single entrypoint for every run.

    python run_batch.py --n 500 --mode baseline --seed 42

Modes `agent` and `naive` are declared but not yet implemented; they arrive with
build steps 4 and 5. The command shape is fixed now so the demo script never has
to change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from data.generator import DEFAULT_SEED, generate_batch
from report.baselines import do_nothing, naive_retry_all
from report.diagnosis import evaluate
from report.diagnosis import render as render_diagnosis
from report.metrics import render

MODES = ("baseline", "naive", "agent", "diagnose")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="run_batch.py",
        description="Run a recovery batch and report measured outcomes.",
    )
    p.add_argument("--n", type=int, default=500, help="batch size (default 500)")
    p.add_argument("--mode", choices=MODES, default="baseline")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument(
        "--split",
        choices=("train", "test", "all"),
        default="test",
        help="report on this split; 'test' is the only honest choice for the deck",
    )
    p.add_argument(
        "--save",
        type=Path,
        default=None,
        help="write the generated batch to this JSON path",
    )
    p.add_argument("--no-llm", action="store_true", help="rules-only path (step 7)")
    args = p.parse_args(argv)

    batch = generate_batch(n=args.n, seed=args.seed)
    split = None if args.split == "all" else args.split

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(batch.model_dump_json(indent=2))
        print(f"batch written to {args.save}", file=sys.stderr)

    if args.mode == "diagnose":
        print(render_diagnosis(evaluate(batch, split=split)))
    elif args.mode == "baseline":
        print(render(do_nothing(batch, split=split)))
    elif args.mode == "naive":
        print(render(naive_retry_all(batch, split=split)))
    else:
        raise SystemExit(
            "mode 'agent' lands in build step 4, once the policy engine and "
            "planner exist."
        )

    if args.split == "train":
        print(
            "  WARNING: these numbers come from the TRAIN split. Do not put them "
            "in the deck.\n",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

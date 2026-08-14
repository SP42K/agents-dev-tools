"""CLI 入口:

  python -m milestone_pipeline run    [--config pipeline.yaml]
  python -m milestone_pipeline status [--config pipeline.yaml]
  python -m milestone_pipeline reset  [--config pipeline.yaml] [--milestone N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Config
from .orchestrator import Orchestrator
from .plan import Plan
from .state import PipelineState


def main() -> None:
    parser = argparse.ArgumentParser(prog="milestone_pipeline")
    parser.add_argument("command", choices=["run", "status", "reset"])
    parser.add_argument("--config", default="pipeline.yaml")
    parser.add_argument("--milestone", type=int, default=None,
                        help="reset 時只清掉指定 milestone 的進度")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load(args.config)

    if args.command == "run":
        asyncio.run(Orchestrator(cfg).run())

    elif args.command == "status":
        plan = Plan.load(cfg.plan_path)
        state = PipelineState.load(cfg.state_file)
        for m in plan.milestones:
            ms = state.milestones.get(str(m.index))
            phase = ms.phase if ms else "pending"
            pr = f" PR#{ms.pr_number}" if ms and ms.pr_number else ""
            rounds = f" round={ms.review_round}" if ms and ms.review_round else ""
            cost = f" ~${ms.cost_usd:.2f}" if ms and ms.cost_usd else ""
            print(f"[{phase:>9}] {m.index}. {m.title}{pr}{rounds}{cost}")

    elif args.command == "reset":
        state = PipelineState.load(cfg.state_file)
        if args.milestone is None:
            cfg.state_file.unlink(missing_ok=True)
            print("已清除全部進度。")
        else:
            state.milestones.pop(str(args.milestone), None)
            state.current = min(state.current, args.milestone)
            state.save(cfg.state_file)
            print(f"已清除 milestone {args.milestone} 的進度。")
    sys.exit(0)


if __name__ == "__main__":
    main()

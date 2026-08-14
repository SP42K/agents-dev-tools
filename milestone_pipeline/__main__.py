"""CLI 入口:

  python -m milestone_pipeline run    [--config pipeline.yaml]
  python -m milestone_pipeline status [--config pipeline.yaml]
  python -m milestone_pipeline retry  [--config pipeline.yaml] --milestone N
  python -m milestone_pipeline reset  [--config pipeline.yaml] [--milestone N]

exit code:0 = 成功;1 = 卡住或流程錯誤(方便 CI 判斷)。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Config
from .orchestrator import Orchestrator, PipelineError
from .plan import Plan
from .state import PH_REVIEW, PH_STUCK, PipelineState


def main() -> None:
    parser = argparse.ArgumentParser(prog="milestone_pipeline")
    parser.add_argument("command", choices=["run", "status", "retry", "reset"])
    parser.add_argument("--config", default="pipeline.yaml")
    parser.add_argument("--milestone", type=int, default=None,
                        help="reset 時只清掉指定 milestone 的進度;"
                             "retry 時指定要重置輪數的 milestone")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load(args.config)

    if args.command == "run":
        try:
            ok = asyncio.run(Orchestrator(cfg).run())
        except PipelineError as e:
            logging.getLogger("pipeline").error("%s", e)
            sys.exit(1)
        sys.exit(0 if ok else 1)

    elif args.command == "status":
        plan = Plan.load(cfg.plan_path)
        state = PipelineState.load(cfg.state_file)
        stuck = False
        for m in plan.milestones:
            ms = state.milestones.get(str(m.index))
            phase = ms.phase if ms else "pending"
            stuck = stuck or phase == PH_STUCK
            pr = f" PR#{ms.pr_number}" if ms and ms.pr_number else ""
            rounds = f" round={ms.review_round}" if ms and ms.review_round else ""
            cost = f" ~${ms.cost_usd:.2f}" if ms and ms.cost_usd else ""
            print(f"[{phase:>9}] {m.index}. {m.title}{pr}{rounds}{cost}")
        sys.exit(1 if stuck else 0)

    elif args.command == "retry":
        # 人工處理完卡住的 PR 之後,把輪數歸零讓 review 迴圈可以再跑,
        # 但保留 PR 編號與 implementer 的 session_id(不丟 context)。
        if args.milestone is None:
            print("retry 需要 --milestone N", file=sys.stderr)
            sys.exit(1)
        state = PipelineState.load(cfg.state_file)
        ms = state.milestones.get(str(args.milestone))
        if ms is None:
            print(f"milestone {args.milestone} 還沒有進度可以重試。", file=sys.stderr)
            sys.exit(1)
        ms.review_round = 0
        if ms.phase == PH_STUCK:
            ms.phase = PH_REVIEW
        state.current = min(state.current, args.milestone)
        state.save(cfg.state_file)
        print(f"已重置 milestone {args.milestone} 的 review 輪數"
              f"(PR#{ms.pr_number}、session 保留),可以重跑 run。")
        sys.exit(0)

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

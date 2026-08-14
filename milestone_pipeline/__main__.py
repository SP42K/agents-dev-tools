"""CLI 入口:

  python -m milestone_pipeline run     [--config pipeline.yaml]
  python -m milestone_pipeline status  [--config pipeline.yaml]
  python -m milestone_pipeline retry   [--config pipeline.yaml] --milestone N
  python -m milestone_pipeline reset   [--config pipeline.yaml] [--milestone N]
  python -m milestone_pipeline approve [--config pipeline.yaml] --milestone N
  python -m milestone_pipeline reject  [--config pipeline.yaml] --milestone N --reason "..."

四個「人工介入」指令語意不同,不要混用:
  retry   —— 卡住(輪數用盡)後重置輪數,保留 PR 與 session
  approve —— 放行停在決策點的 milestone,繼續往下跑
  reject  —— 打回,把 reason 當成新一輪 review 意見交給 implementer
  reset   —— 整個清掉重來

exit code:0 = 成功;1 = 停下來等人或流程錯誤(方便 CI 判斷)。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import Config
from .notify import R_AGENT_ERROR, R_MERGE_GATE, R_UNRESOLVED
from .orchestrator import Orchestrator, PipelineError
from .plan import Plan
from .state import (PH_AWAIT_HUMAN, PH_MERGE, PH_REVIEW, PH_STUCK,
                    MilestoneState, PipelineState)


def main() -> None:
    parser = argparse.ArgumentParser(prog="milestone_pipeline")
    parser.add_argument(
        "command",
        choices=["run", "status", "retry", "reset", "approve", "reject"],
    )
    parser.add_argument("--config", default="pipeline.yaml")
    parser.add_argument("--milestone", type=int, default=None,
                        help="reset 時只清掉指定 milestone 的進度;"
                             "retry / approve / reject 時指定目標 milestone")
    parser.add_argument("--reason", default=None,
                        help="reject 時給 implementer 的意見")
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
        paused = False
        for m in plan.milestones:
            ms = state.milestones.get(str(m.index))
            phase = ms.phase if ms else "pending"
            paused = paused or phase in (PH_STUCK, PH_AWAIT_HUMAN)
            pr = f" PR#{ms.pr_number}" if ms and ms.pr_number else ""
            rounds = f" round={ms.review_round}" if ms and ms.review_round else ""
            cost = f" ~${ms.cost_usd:.2f}" if ms and ms.cost_usd else ""
            why = f" ({ms.await_reason})" if ms and ms.await_reason else ""
            print(f"[{phase:>11}] {m.index}. {m.title}{pr}{rounds}{cost}{why}")
            # 停在決策點的,把當初的內容一起印出來,不用去翻 log
            if ms and ms.phase == PH_AWAIT_HUMAN and ms.await_payload:
                for line in ms.await_payload.splitlines():
                    print(f"              | {line}")
                print(f"              放行: python -m milestone_pipeline approve "
                      f"--milestone {m.index} --config {args.config}")
                print(f"              打回: python -m milestone_pipeline reject "
                      f'--milestone {m.index} --reason "..." --config {args.config}')
        sys.exit(1 if paused else 0)

    elif args.command == "retry":
        # 人工處理完卡住的 PR 之後,把輪數歸零讓 review 迴圈可以再跑,
        # 但保留 PR 編號與 implementer 的 session_id(不丟 context)。
        state, ms = _require_milestone(cfg, args)
        ms.review_round = 0
        if ms.phase == PH_STUCK:
            ms.phase = PH_REVIEW
        _rewind_and_save(cfg, state, args.milestone)
        print(f"已重置 milestone {args.milestone} 的 review 輪數"
              f"(PR#{ms.pr_number}、session 保留),可以重跑 run。")
        sys.exit(0)

    elif args.command == "approve":
        state, ms = _require_milestone(cfg, args)
        if ms.phase != PH_AWAIT_HUMAN:
            print(f"milestone {args.milestone} 不在等待決策的狀態"
                  f"(目前 phase={ms.phase}),不需要 approve。", file=sys.stderr)
            sys.exit(1)

        reason = ms.await_reason
        if reason in (R_MERGE_GATE, R_UNRESOLVED):
            # 人裁決:接受現狀並放行 merge
            ms.phase = PH_MERGE
            ms.merge_approved = True
            nxt = "接著會直接 merge"
        elif reason == R_AGENT_ERROR:
            # 多半是加了預算/輪數後要接著跑,回到當初中斷的 phase
            ms.phase = ms.await_prev_phase or PH_REVIEW
            nxt = f"接著會從 {ms.phase} 階段續跑(session 保留)"
        else:
            ms.phase = ms.await_prev_phase or PH_REVIEW
            nxt = f"接著會從 {ms.phase} 階段續跑"

        _clear_await(ms)
        _rewind_and_save(cfg, state, args.milestone)
        print(f"已放行 milestone {args.milestone}({reason});{nxt}。"
              f"執行 `run` 繼續。")
        sys.exit(0)

    elif args.command == "reject":
        if not args.reason:
            print("reject 需要 --reason \"...\"(會當成 review 意見交給 "
                  "implementer)", file=sys.stderr)
            sys.exit(1)
        state, ms = _require_milestone(cfg, args)
        if ms.phase not in (PH_AWAIT_HUMAN, PH_STUCK, PH_MERGE):
            print(f"milestone {args.milestone} 目前 phase={ms.phase},"
                  "沒有可以打回的東西。", file=sys.stderr)
            sys.exit(1)

        ms.phase = PH_REVIEW
        ms.human_feedback = args.reason
        ms.merge_approved = False
        # 人已經介入接手,給新的輪數預算(與 retry 同語意)
        ms.review_round = 0
        _clear_await(ms)
        _rewind_and_save(cfg, state, args.milestone)
        print(f"已打回 milestone {args.milestone}。下一輪會把你的意見"
              "直接交給 implementer(跳過 reviewer),執行 `run` 繼續。")
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


# -- 共用小工具 --------------------------------------------------------------

def _require_milestone(cfg: Config, args) -> tuple[PipelineState, MilestoneState]:
    """取出指定 milestone 的狀態,順便驗參數。找不到就結束。

    連 state 一起回傳:改完要存檔的是整個 state,呼叫端兩個都會用到。
    """
    if args.milestone is None:
        print(f"{args.command} 需要 --milestone N", file=sys.stderr)
        sys.exit(1)
    state = PipelineState.load(cfg.state_file)
    ms = state.milestones.get(str(args.milestone))
    if ms is None:
        print(f"milestone {args.milestone} 還沒有進度可以操作。", file=sys.stderr)
        sys.exit(1)
    return state, ms


def _clear_await(ms: MilestoneState) -> None:
    ms.await_reason = None
    ms.await_payload = None
    ms.await_prev_phase = None


def _rewind_and_save(cfg: Config, state: PipelineState, index: int) -> None:
    """把游標倒回這個 milestone(才會被 run 重新處理到)並存檔。"""
    state.current = min(state.current, index)
    state.save(cfg.state_file)


if __name__ == "__main__":
    main()

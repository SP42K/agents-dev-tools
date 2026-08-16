"""CLI 入口:

  python -m milestone_pipeline run     [--config pipeline.yaml]
  python -m milestone_pipeline status  [--config pipeline.yaml]
  python -m milestone_pipeline retry   [--config pipeline.yaml] --milestone N
  python -m milestone_pipeline reset   [--config pipeline.yaml] [--milestone N]
  python -m milestone_pipeline approve [--config pipeline.yaml] --milestone N
  python -m milestone_pipeline reject  [--config pipeline.yaml] --milestone N --reason "..."
  python -m milestone_pipeline guard   [--config pipeline.yaml]
  python -m milestone_pipeline guards  [--dir .]
  python -m milestone_pipeline tgbot   [--dir .]

`guard` 起一個守護 agent 代替人顧這條 pipeline —— 它被關掉了所有寫入工具,
只能 approve / reject(見 guard.py)。

`guards`(複數)是唯讀報表:掃一個目錄底下所有 config,配上 tmux 裡活著的
守護 agent,一條 pipeline 印一行。守護 agent 多半跑在別台,所以典型用法是
`ssh mac 'cd ~/Documents/agents-dev-tools && .venv/bin/python -m
milestone_pipeline guards'`。它不吃 `--config`,吃 `--dir`。

`tgbot` 是同一份東西的 Telegram 前端:在手機上下 `/guards` / `/approve` /
`/reject`,決策成功後自己重啟 `run`。存取控制只有 `TELEGRAM_CHAT_ID` 白名單,
兩個環境變數缺一個就拒絕啟動(見 tgbot.py)。

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
from pathlib import Path

from . import guard, lock, tgbot
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
        choices=["run", "status", "retry", "reset", "approve", "reject",
                 "guard", "guards", "tgbot"],
    )
    parser.add_argument("--config", default="pipeline.yaml")
    parser.add_argument("--dir", default=".",
                        help="guards 掃哪個目錄底下的 config(預設當前目錄)")
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

    # `guards` 掃的是一整個目錄的 config,不吃 `--config` —— 一定要在下面那行
    # `Config.load(args.config)` **之前**分流,否則會先被預設的 `pipeline.yaml`
    # (repo.path 是佔位字串)擋下來。
    if args.command == "guards":
        sys.exit(_guards(Path(args.dir)))

    # 同上:`tgbot` 是跨專案的(一個 bot 管全部),吃 `--dir` 不吃 `--config`。
    if args.command == "tgbot":
        sys.exit(tgbot.serve(Path(args.dir)))

    cfg = Config.load(args.config)

    if args.command == "run":
        # 鎖圈住整段 run:兩個 orchestrator 併跑會互相覆蓋存檔(見 lock.py)。
        with lock.exclusive(cfg.state_file):
            try:
                ok = asyncio.run(Orchestrator(cfg).run())
            except PipelineError as e:
                logging.getLogger("pipeline").error("%s", e)
                sys.exit(1)
        sys.exit(0 if ok else 1)

    elif args.command == "guard":
        sys.exit(guard.run(args.config))

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
        # 人已接手 → 清 reviewer_seen(見 MilestoneState 的不變式):人工處理過
        # 的 PR 內容已經不是先前那次 review 掃過的東西,下一輪要重新完整掃描。
        ms.reviewer_seen = False
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
        # 同上的不變式(人已接手)。reject 的典型用法是「接續你被中斷的修復」,implementer
        # 會在那一輪寫大量新程式碼,而人的意見範圍通常比實際改動窄很多 ——
        # 先前那次 review 涵蓋不到,下一輪必須是不受限的完整掃描。
        ms.reviewer_seen = False
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


def _guards(workdir: Path) -> int:
    """印出所有 pipeline 的一行摘要。exit code 沿用 `status`:有東西等人就回 1。"""
    if not guard._resolve("tmux"):
        # Windows 走這條(守護 agent 在這裡是前景跑的,沒有 session 可列)。
        # 仍然把讀得到的 state 印出來 —— 降級,不是錯誤。
        print("(沒有 tmux,列不出守護 agent 的 session,只印各條 pipeline 的存檔狀態)")
    rows = guard.collect(workdir)
    if not rows:
        print(f"{workdir.resolve()} 底下沒有找到任何 pipeline config。")
        return 0
    for row in rows:
        for line in row.lines:
            print(line)
    return 1 if any(r.attention for r in rows) else 0


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

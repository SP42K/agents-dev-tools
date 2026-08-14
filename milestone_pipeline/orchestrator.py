"""主迴圈:milestone → 實作 → 開 PR → (review ↔ fix)* → merge → 下一個。

確定性的事(找 PR、數輪數、merge、存進度)由這裡的 code 做;
需要智慧的事(實作、review、修復、回覆)交給 agent。
"""
from __future__ import annotations

import logging

from .config import Config
from .gh import Gh
from .implementer import Implementer
from .plan import Milestone, Plan
from .prompts import fix_prompt, implement_prompt
from .reviewer import make_reviewer
from .state import (PH_DONE, PH_IMPLEMENT, PH_MERGE, PH_REVIEW, PH_STUCK,
                    MilestoneState, PipelineState)

log = logging.getLogger("pipeline")


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.plan = Plan.load(cfg.plan_path)
        self.state = PipelineState.load(cfg.state_file)
        self.gh = Gh(cfg.repo_path)
        self.reviewer = make_reviewer(cfg.reviewer, cfg.repo_path)

    def _save(self) -> None:
        self.state.save(self.cfg.state_file)

    async def run(self) -> None:
        for milestone in self.plan.milestones:
            if milestone.index < self.state.current:
                continue
            ms = self.state.ms(milestone.index)
            if ms.phase == PH_DONE:
                continue

            log.info("=== Milestone %d: %s (phase=%s) ===",
                     milestone.index, milestone.title, ms.phase)
            await self._run_milestone(milestone, ms)

            if ms.phase == PH_STUCK:
                log.error("Milestone %d 卡住(超過 review 輪數上限),"
                          "請人工處理 PR #%s 後重跑。",
                          milestone.index, ms.pr_number)
                return

            self.state.current = milestone.index + 1
            self._save()

        log.info("所有 milestone 完成 🎉")

    async def _run_milestone(self, m: Milestone, ms: MilestoneState) -> None:
        async with Implementer(self.cfg.implementer, self.cfg.repo_path,
                               resume_session_id=ms.session_id) as imp:

            # -- Phase 1: 實作 + 開 PR ---------------------------------------
            if ms.phase == PH_IMPLEMENT:
                self.gh.checkout_base(self.cfg.base_branch, self.cfg.remote)
                result = await imp.ask(implement_prompt(
                    self.plan.preamble, m.title, m.body,
                    m.branch, self.cfg.base_branch, m.index))
                ms.session_id = imp.session_id
                ms.cost_usd = result.cost_usd
                ms.branch = m.branch

                pr = self.gh.find_pr(m.branch)
                if pr is None:
                    raise RuntimeError(
                        f"implementer 回報完成但找不到 {m.branch} 的 open PR;"
                        f"agent 輸出:\n{result.text[-2000:]}")
                ms.pr_number = pr
                ms.phase = PH_REVIEW
                self._save()
                log.info("PR #%d 已開(branch: %s)", pr, m.branch)

            # -- Phase 2: review ↔ fix 迴圈 ----------------------------------
            excerpt = f"## {m.title}\n{m.body}"
            while (ms.phase == PH_REVIEW
                   and ms.review_round < self.cfg.loop.max_review_rounds):
                ms.review_round += 1
                self._save()
                log.info("Review 第 %d/%d 輪…",
                         ms.review_round, self.cfg.loop.max_review_rounds)

                review = await self.reviewer.review(
                    ms.pr_number, ms.review_round, excerpt)
                ms.cost_usd += review.cost_usd

                if review.approved:
                    log.info("Reviewer APPROVE ✅")
                    ms.phase = PH_MERGE
                    self._save()
                    break

                log.info("Reviewer 要求修改,交回 implementer(同一個 session)…")
                fix = await imp.ask(fix_prompt(
                    review.feedback, ms.pr_number, ms.review_round))
                ms.session_id = imp.session_id
                ms.cost_usd = max(ms.cost_usd, fix.cost_usd)

                if self.cfg.loop.compact_between_rounds:
                    await imp.compact()
                self._save()

            if ms.phase == PH_REVIEW:  # 輪數用完仍未 approve
                ms.phase = PH_STUCK
                self._save()
                self.gh.pr_comment(
                    ms.pr_number,
                    f"⚠️ 自動流程:review 已達 "
                    f"{self.cfg.loop.max_review_rounds} 輪上限仍未 approve,"
                    "暫停等待人工介入。")
                return

        # -- Phase 3: merge(implementer session 已關閉,由 orchestrator 確定性執行)
        if ms.phase == PH_MERGE:
            self.gh.merge(ms.pr_number, self.cfg.loop.merge_method)
            ms.phase = PH_DONE
            self._save()
            log.info("PR #%d 已 merge(%s),milestone %d 完成。累計花費 ~$%.2f",
                     ms.pr_number, self.cfg.loop.merge_method,
                     m.index, ms.cost_usd)

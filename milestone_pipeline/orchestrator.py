"""主迴圈:milestone → 實作 → 開 PR → (review ↔ fix)* → merge → 下一個。

確定性的事(找 PR、數輪數、merge、存進度、判斷該不該停下來問人)由這裡的
code 做;需要智慧的事(實作、review、修復、回覆)交給 agent。

停下來等人時走 park & notify:存檔 → 通知 → 乾淨退出,人再用
`approve` / `reject` 恢復。刻意不在這裡 block 等 input(),
那會讓 pipeline 沒辦法無人值守跑,而且 crash 就前功盡棄。
"""
from __future__ import annotations

import logging

from .config import Config
from .gh import Gh
from .implementer import Implementer
from .notify import (R_AGENT_ERROR, R_MERGE_GATE, R_STUCK, R_UNRESOLVED,
                     Decision, make_notifier)
from .plan import Milestone, Plan
from .prompts import (fix_prompt, has_unresolved_marker, implement_prompt,
                      parse_unresolved)
from .reviewer import make_reviewer
from .state import (PH_AWAIT_HUMAN, PH_DONE, PH_IMPLEMENT, PH_MERGE, PH_REVIEW,
                    PH_STUCK, MilestoneState, PipelineState)

log = logging.getLogger("pipeline")

# 停下來等人的 phase,run() 遇到就收工
_PAUSED = (PH_STUCK, PH_AWAIT_HUMAN)


class PipelineError(RuntimeError):
    """流程中止:狀態與現實不符,人不介入就沒辦法繼續。"""


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.plan = Plan.load(cfg.plan_path)
        self.state = PipelineState.load(cfg.state_file)
        self.gh = Gh(cfg.repo_path)
        self.reviewer = make_reviewer(cfg.reviewer, cfg.repo_path)
        self.notifier = make_notifier(cfg.notify, cfg.repo_path)

    def _save(self) -> None:
        self.state.save(self.cfg.state_file)

    # -- 決策點 --------------------------------------------------------------

    def _park(self, m: Milestone, ms: MilestoneState, reason: str,
              detail: str, prev_phase: str | None = None) -> None:
        """停在決策點:存檔 + 通知。通知失敗不影響存檔(MultiNotifier 會吞例外)。"""
        ms.phase = PH_AWAIT_HUMAN
        ms.await_reason = reason
        ms.await_payload = detail
        ms.await_prev_phase = prev_phase
        self._save()

        decision = Decision(
            milestone_index=m.index,
            milestone_title=m.title,
            reason=reason,
            detail=detail,
            config_hint=self.cfg.config_hint,
            pr_number=ms.pr_number,
            pr_url=self._pr_url(ms.pr_number),
            cost_usd=ms.cost_usd,
        )
        log.warning("⏸ Milestone %d 停下來等人(%s)。放行:%s",
                    m.index, reason, decision.approve_cmd)
        self.notifier.notify(decision)

    def _pr_url(self, pr_number: int | None) -> str | None:
        if pr_number is None:
            return None
        try:
            return self.gh.pr_view(pr_number, "url").get("url")
        except Exception:  # noqa: BLE001 - 取不到 URL 不該影響通知本身
            return None

    # -- 主流程 --------------------------------------------------------------

    async def run(self) -> bool:
        """跑完所有 milestone。全部完成回傳 True,停下來等人回傳 False。"""
        for milestone in self.plan.milestones:
            if milestone.index < self.state.current:
                continue
            ms = self.state.ms(milestone.index)
            if ms.phase == PH_DONE:
                continue

            log.info("=== Milestone %d: %s (phase=%s) ===",
                     milestone.index, milestone.title, ms.phase)
            await self._run_milestone(milestone, ms)

            if ms.phase in _PAUSED:
                self._report_paused(milestone, ms)
                return False

            self.state.current = milestone.index + 1
            self._save()

        log.info("所有 milestone 完成 🎉")
        return True

    def _report_paused(self, m: Milestone, ms: MilestoneState) -> None:
        if ms.phase == PH_STUCK:
            log.error("Milestone %d 卡住(超過 review 輪數上限),"
                      "請人工處理 PR #%s,再用 "
                      "`retry --milestone %d` 重置輪數後重跑。",
                      m.index, ms.pr_number, m.index)
        else:
            log.error("Milestone %d 等待決策(%s)。"
                      "看內容:`status --config %s`;"
                      "放行:`approve --milestone %d`;"
                      "打回:`reject --milestone %d --reason \"...\"`",
                      m.index, ms.await_reason, self.cfg.config_hint,
                      m.index, m.index)

    async def _run_milestone(self, m: Milestone, ms: MilestoneState) -> None:
        async with Implementer(self.cfg.implementer, self.cfg.repo_path,
                               resume_session_id=ms.session_id) as imp:

            # -- Phase 1: 實作 + 開 PR ---------------------------------------
            if ms.phase == PH_IMPLEMENT:
                self.gh.checkout_base(self.cfg.base_branch, self.cfg.remote)
                result = await imp.ask(implement_prompt(
                    self.plan.preamble, m.title, m.body,
                    m.branch, self.cfg.base_branch, m.index, self.cfg.remote))
                ms.session_id = imp.session_id
                # SDK 回傳的是該 session 的累計花費,所以覆寫而非累加
                ms.implementer_cost_usd = result.cost_usd
                ms.branch = m.branch
                self._save()

                if result.is_error:
                    detail = (f"implementer 在實作階段回報錯誤"
                              f"(subtype=`{result.subtype}`)。\n\n"
                              f"最後輸出:\n\n```\n{result.text[-2000:]}\n```")
                    if self.cfg.loop.gate_on_agent_error:
                        # 多半是預算/輪數用完:加額度後 approve 就能接著跑,
                        # 不必整個 milestone 重來(session_id 已存)。
                        self._park(m, ms, R_AGENT_ERROR, detail, PH_IMPLEMENT)
                        return
                    raise PipelineError(detail)

                pr = self.gh.find_pr(m.branch)
                if pr is None:
                    raise PipelineError(
                        f"implementer 回報完成但找不到 {m.branch} 的 open PR;"
                        f"agent 輸出:\n{result.text[-2000:]}")
                ms.pr_number = pr
                ms.phase = PH_REVIEW
                self._save()
                log.info("PR #%d 已開(branch: %s)", pr, m.branch)

            if ms.pr_number is None:
                raise PipelineError(
                    f"milestone {m.index} 的狀態是 {ms.phase} 但沒有 PR 編號;"
                    f"請用 `reset --milestone {m.index}` 重跑這個 milestone。")

            # -- Phase 2: review ↔ fix 迴圈 ----------------------------------
            excerpt = f"## {m.title}\n{m.body}"
            while (ms.phase == PH_REVIEW
                   and ms.review_round < self.cfg.loop.max_review_rounds):
                ms.review_round += 1
                self._save()

                # 人工 reject 的理由直接當這一輪的意見,省掉一次 reviewer 呼叫
                if ms.human_feedback:
                    log.info("第 %d 輪:用人工 reject 的意見,跳過 reviewer。",
                             ms.review_round)
                    feedback = f"(以下是人工 reviewer 的意見)\n\n{ms.human_feedback}"
                    ms.human_feedback = None
                    self._save()
                else:
                    log.info("Review 第 %d/%d 輪…",
                             ms.review_round, self.cfg.loop.max_review_rounds)
                    review = await self.reviewer.review(
                        ms.pr_number, ms.review_round, excerpt)
                    ms.reviewer_cost_usd += review.cost_usd
                    self._save()

                    if review.is_error:
                        detail = (f"reviewer 在第 {ms.review_round} 輪回報錯誤"
                                  f"(可能超過 max_turns 或 max_budget_usd)。\n\n"
                                  f"輸出:\n\n```\n{review.feedback[-2000:]}\n```")
                        if self.cfg.loop.gate_on_agent_error:
                            self._park(m, ms, R_AGENT_ERROR, detail, PH_REVIEW)
                            return
                        raise PipelineError(detail)

                    if review.approved:
                        log.info("Reviewer APPROVE ✅")
                        ms.phase = PH_MERGE
                        self._save()
                        break

                    feedback = review.feedback

                log.info("Reviewer 要求修改,交回 implementer(同一個 session)…")
                fix = await imp.ask(fix_prompt(
                    feedback, ms.pr_number, ms.review_round,
                    ms.branch or m.branch, self.cfg.remote))
                ms.session_id = imp.session_id
                ms.implementer_cost_usd = fix.cost_usd
                self._save()

                if fix.is_error:
                    detail = (f"implementer 在第 {ms.review_round} 輪修復時回報錯誤"
                              f"(subtype=`{fix.subtype}`)。\n\n"
                              f"輸出:\n\n```\n{fix.text[-2000:]}\n```")
                    if self.cfg.loop.gate_on_agent_error:
                        self._park(m, ms, R_AGENT_ERROR, detail, PH_REVIEW)
                        return
                    raise PipelineError(detail)

                # implementer 自陳與 reviewer 有爭點 → 讓人裁決,不要讓它們互相說服
                if not has_unresolved_marker(fix.text):
                    log.warning("implementer 第 %d 輪沒有輸出 UNRESOLVED 標記,"
                                "視為沒有分歧(契約見 prompts.fix_prompt)。",
                                ms.review_round)
                elif parse_unresolved(fix.text) and self.cfg.loop.gate_on_unresolved:
                    detail = ("implementer 回報與 reviewer 有未解決的分歧。\n\n"
                              f"**Reviewer 第 {ms.review_round} 輪意見**:\n\n"
                              f"{feedback[-1500:]}\n\n"
                              f"**Implementer 的回應**:\n\n{fix.text[-1500:]}")
                    self._park(m, ms, R_UNRESOLVED, detail, PH_REVIEW)
                    return

                if self.cfg.loop.compact_between_rounds:
                    await imp.compact()

            if ms.phase == PH_REVIEW:  # 輪數用完仍未 approve
                ms.phase = PH_STUCK
                ms.await_reason = R_STUCK
                self._save()
                self.notifier.notify(Decision(
                    milestone_index=m.index,
                    milestone_title=m.title,
                    reason=R_STUCK,
                    detail=(f"review 已達 {self.cfg.loop.max_review_rounds} 輪上限"
                            "仍未 approve。人工處理 PR 後,用 "
                            f"`retry --milestone {m.index}` 重置輪數再跑。"),
                    config_hint=self.cfg.config_hint,
                    pr_number=ms.pr_number,
                    pr_url=self._pr_url(ms.pr_number),
                    cost_usd=ms.cost_usd,
                ))
                return

            # -- Phase 3: merge 前的人工關卡 ---------------------------------
            # 在 implementer session 還開著時判斷,但 merge 本身留到 session
            # 關掉之後由 orchestrator 確定性執行。
            if (ms.phase == PH_MERGE
                    and not ms.merge_approved
                    and self.cfg.loop.needs_human_merge(m.index)):
                self._park(m, ms, R_MERGE_GATE,
                           f"reviewer 已 APPROVE(第 {ms.review_round} 輪),"
                           "依設定 merge 前需人工放行。\n\n"
                           "建議先確認 milestone 的驗收條件是否真的達成"
                           "(有些驗收要接真實 API 或實際部署才驗得了)。",
                           PH_MERGE)
                return

        # -- Phase 4: merge(implementer session 已關閉)---------------------
        if ms.phase == PH_MERGE:
            self.gh.merge(ms.pr_number, self.cfg.loop.merge_method)
            ms.phase = PH_DONE
            self._save()
            log.info("PR #%d 已 merge(%s),milestone %d 完成。累計花費 ~$%.2f",
                     ms.pr_number, self.cfg.loop.merge_method,
                     m.index, ms.cost_usd)

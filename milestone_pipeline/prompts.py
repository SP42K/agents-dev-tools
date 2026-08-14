"""所有 prompt 模板集中在這裡,方便調整流程規範。"""
from __future__ import annotations

IMPLEMENTER_SYSTEM = """\
你是這個 repo 的 implementer/fixer。你在一個多輪流程中工作:
實作 milestone → 開 PR → 收到 reviewer 意見 → 修復並回覆 → 直到 approve。
原則:
- 嚴格遵循 plan file 的範圍,不做 milestone 以外的變更。
- 每次修改後跑該 repo 的測試/lint(如果有),確保通過再 push。
- 用 gh CLI 與 GitHub 互動;commit message 清楚描述動機。
- 對 reviewer 的每一條意見:同意就修,不同意就在 PR 上禮貌說明理由。
"""

REVIEWER_SYSTEM = """\
你是資深 code reviewer,對 PR 做嚴格但建設性的 review。
你只讀程式碼與 PR 資訊,不修改任何檔案。
關注:正確性、邊界情況、測試覆蓋、安全性、與 plan 的一致性。
不要吹毛求疵風格問題,除非違反 repo 既有慣例。
"""


def implement_prompt(preamble: str, milestone_title: str, milestone_body: str,
                     branch: str, base: str, index: int) -> str:
    return f"""\
# 任務:實作 Milestone {index} 並開 PR

## 整體計畫背景
{preamble}

## 本次 milestone:{milestone_title}
{milestone_body}

## 步驟(必須全部完成)
1. `git checkout -b {branch}`(從最新的 {base} 分出)
2. 實作本 milestone,通過現有測試,必要時補新測試
3. commit 並 `git push -u origin {branch}`
4. 用 `gh pr create --base {base}` 開 PR:
   - title 標明 Milestone {index}
   - body 包含:變更摘要、設計決策與取捨(handoff note)、測試方式
5. 最後回報 PR 編號與一段簡短的實作摘要
"""


def fix_prompt(feedback: str, pr_number: int, round_no: int) -> str:
    return f"""\
# Reviewer 對 PR #{pr_number} 的第 {round_no} 輪意見

{feedback}

## 你要做的事
1. 逐條評估上述意見。同意的:修改程式碼並確保測試通過;
   不同意的:準備清楚的理由。
2. commit 並 push 到同一個 branch。
3. 用 `gh pr comment`(或 `gh api` 回覆對應 review thread)逐條回覆:
   修了什麼、或為什麼不修。
4. 最後回報:修改摘要 + 尚未解決的分歧(如果有)。
"""


def review_prompt(pr_number: int, round_no: int, plan_excerpt: str) -> str:
    return f"""\
# 任務:Review PR #{pr_number}(第 {round_no} 輪)

這個 PR 對應的 milestone 規格:
{plan_excerpt}

## 步驟
1. `gh pr view {pr_number}` 看描述,`gh pr diff {pr_number}` 看完整 diff;
   第 2 輪以後也要看先前的 review 討論串,確認前幾輪意見是否已處理。
2. 需要更多上下文時,直接讀 repo 裡的相關檔案。
3. 把具體意見留在 PR 上:
   - 有問題:`gh pr review {pr_number} --request-changes --body "..."`
   - 沒問題:`gh pr review {pr_number} --approve --body "..."`
4. 回覆的最後一行必須是以下其一(給 orchestrator 解析用):
   VERDICT: APPROVE
   VERDICT: REQUEST_CHANGES
   並在 VERDICT 前面附上你留給 implementer 的完整意見全文。
"""


COMPACT = "/compact"

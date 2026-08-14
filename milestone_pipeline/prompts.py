"""所有 prompt 模板集中在這裡,方便調整流程規範。

這裡同時放 `UNRESOLVED` 契約的**兩半**(模板 + 解析),刻意跟 `VERDICT`
的作法不同 —— `VERDICT` 的模板在這裡、regex 在 reviewer.py,改一邊很容易
忘了另一邊。新契約一律兩半同檔,改動時看得到彼此。
"""
from __future__ import annotations

import re

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
                     branch: str, base: str, index: int,
                     remote: str = "origin") -> str:
    return f"""\
# 任務:實作 Milestone {index} 並開 PR

## 整體計畫背景
{preamble}

## 本次 milestone:{milestone_title}
{milestone_body}

## 步驟(必須全部完成)
1. `git checkout -b {branch}`(從最新的 {base} 分出)
2. 實作本 milestone,通過現有測試,必要時補新測試
3. commit 並 `git push -u {remote} {branch}`
4. 用 `gh pr create --base {base}` 開 PR:
   - title 標明 Milestone {index}
   - body 包含:變更摘要、設計決策與取捨(handoff note)、測試方式
5. 最後回報 PR 編號與一段簡短的實作摘要
"""


def fix_prompt(feedback: str, pr_number: int, round_no: int,
               branch: str, remote: str = "origin") -> str:
    return f"""\
# Reviewer 對 PR #{pr_number} 的第 {round_no} 輪意見

{feedback}

## 你要做的事
1. 逐條評估上述意見。同意的:修改程式碼並確保測試通過;
   不同意的:準備清楚的理由。
2. commit 並 `git push {remote} {branch}`(同一個 branch)。
3. 用 `gh pr comment`(或 `gh api` 回覆對應 review thread)逐條回覆:
   修了什麼、或為什麼不修。
4. 最後回報:修改摘要 + 尚未解決的分歧(如果有)。
5. 回覆的最後一行必須是以下其一,獨立成行(給 orchestrator 解析用):
   UNRESOLVED: YES     ← 你與 reviewer 有沒能達成共識的爭點,需要人來裁決
   UNRESOLVED: NO      ← 意見都處理完了,沒有懸而未決的分歧
   選 YES 時,請在這一行**之前**清楚寫出爭點是什麼、雙方各自的理由。
"""


# `UNRESOLVED` 契約的另一半。只認自成一行的形式,取最後一個 —— 與 VERDICT 同規則。
_UNRESOLVED_RE = re.compile(r"^\s*UNRESOLVED:\s*(YES|NO)\s*$", re.MULTILINE)


def parse_unresolved(text: str) -> bool:
    """有沒有未解決的分歧。解析不到時回 False(視為沒有分歧)。

    這裡刻意**不**照 VERDICT 的保守方向。VERDICT 解析失敗要 fail-closed
    (擋住 merge),因為它是唯一的品質關卡;UNRESOLVED 解析失敗則 fail-open,
    因為下游還有 reviewer 的 APPROVE 擋著,而且 agent 漏掉結尾標記很常見,
    每次都停下來問人會讓無人值守跑不起來。呼叫端要記得 log 警告。
    """
    found = _UNRESOLVED_RE.findall(text)
    if not found:
        return False
    return found[-1] == "YES"


def has_unresolved_marker(text: str) -> bool:
    """agent 到底有沒有輸出這個標記(用來決定要不要記警告)。"""
    return bool(_UNRESOLVED_RE.search(text))


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
   注意:如果這個 PR 是用同一個 GitHub 帳號開的,GitHub 會拒絕你 review
   自己的 PR(`Can not approve your own pull request`)。遇到這個錯誤時,
   改用 `gh pr comment {pr_number} --body "..."` 把同樣的意見全文留在 PR 上,
   不要重試 `gh pr review`,也不要因此中止任務。
4. 回覆的最後一行必須是以下其一,獨立成行(給 orchestrator 解析用):
   VERDICT: APPROVE
   VERDICT: REQUEST_CHANGES
   並在 VERDICT 前面附上你留給 implementer 的完整意見全文。
"""


COMPACT = "/compact"

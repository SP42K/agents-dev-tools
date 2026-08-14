# milestone-pipeline

純 SDK 版「一人公司」開發流程 orchestrator:

```
plan.md 的每個 milestone:
  implementer(預設 fable,持久 session)實作 → push → 開 PR
  → reviewer(預設 opus,每輪 fresh session)review、意見留在 PR 上
  → implementer 在「同一個 session」修復、逐條回覆(輪間可 /compact)
  → 迴圈直到 APPROVE 或達輪數上限
  → orchestrator 確定性地 merge → 下一個 milestone
```

## 架構重點

- **Implementer/fixer 共享 context window**:一個 milestone 一個
  `ClaudeSDKClient` 持久 session,fixer 記得自己的設計決策;
  `compact_between_rounds: true` 會在每輪修復後下 `/compact` 壓縮上下文。
- **Reviewer 每輪 fresh**:不被 implementer 思路污染;review 全文
  同時留在 PR 上(`gh pr review`),溝通記錄可追溯。
- **確定性控制**:輪數上限、預算上限(`max_budget_usd`)、merge
  都由 orchestrator 的 code 控制,不靠 prompt 自律。
- **可中斷/恢復**:進度與 implementer session_id 存在
  `.pipeline-state.json`,crash 或 Ctrl-C 後重跑 `run` 會從斷點續跑
  (session 用 `resume` 接回同一個 context)。
- **模型可抽換**:`pipeline.yaml` 改 `implementer.model` /
  `reviewer.model` 即可(alias 或完整 model id)。
- **可演化成混合版**:`reviewer.type: actions` 切換成「review 由
  GitHub Actions workflow 執行、script 輪詢結果」;implementer 側不變。

## 前置需求

1. Python 3.10+,`pip install -r requirements.txt`
2. [Claude Code CLI](https://code.claude.com) 已安裝並登入
   (或設好 `ANTHROPIC_API_KEY`)—— Agent SDK 依賴它
3. `gh` CLI 已 `gh auth login`,對目標 repo 有 push/merge 權限
4. 目標 repo 已 clone 到本機

## 使用

```bash
cp pipeline.yaml my-pipeline.yaml   # 改 repo.path、plan.path、模型等
python -m milestone_pipeline run    --config my-pipeline.yaml
python -m milestone_pipeline status --config my-pipeline.yaml
python -m milestone_pipeline reset  --config my-pipeline.yaml --milestone 2
```

Plan file 格式見 `plan.example.md`:每個 `## ` 標題一個 milestone,
第一個標題前的內容是整體背景(每個 milestone 都會附給 implementer)。

## 安全與成本開關

- `implementer.permission_mode`:`acceptEdits`(預設)自動核准檔案編輯;
  `bypassPermissions` 全自動但建議只在隔離環境(container/VM)使用。
- `max_budget_usd`:implementer 以「每個 milestone 的 session」計,
  reviewer 以「每輪」計,超過即中止該次呼叫。
- `loop.max_review_rounds`:防止兩個 agent 無限對話;超過上限會在
  PR 留言並停下等人工介入(`status` 會顯示 `stuck`)。

## 已知簡化(骨架階段)

- verdict 靠 reviewer 輸出 `VERDICT: APPROVE|REQUEST_CHANGES` 最後一行
  解析;解析失敗時保守視為要求修改。
- reviewer 的行內 comment 與 fixer 的 thread 回覆用 `gh pr comment`
  簡化處理;要做到逐 thread 回覆可改用 `gh api` 的 review threads API。
- 未處理 merge conflict(後面的 milestone 疊在最新 base 上,
  正常序列執行時不會發生;人工介入過的 PR 需自行 rebase)。
- 混合版(`reviewer.type: actions`)需要你在 repo 裝好
  `on: pull_request` 觸發的 claude-code-action review workflow,
  這裡只實作了輪詢端。

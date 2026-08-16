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
python -m milestone_pipeline retry  --config my-pipeline.yaml --milestone 2
python -m milestone_pipeline reset  --config my-pipeline.yaml --milestone 2
python -m milestone_pipeline guard  --config my-pipeline.yaml   # 起一個守護 agent
python -m milestone_pipeline guards                             # 全部看一遍
```

`run` / `status` / `guards` 在流程卡住時 exit code 為 1,方便 CI 判斷。

| 指令 | 作用 |
| --- | --- |
| `run` | 從斷點續跑到全部 milestone 完成 |
| `status` | 列出每個 milestone 的 phase / PR / 輪數 / 花費 |
| `guard` | 起一個守護 agent(tmux session `guard-<config stem>`)代替人在 park 點做決策;它被關掉了所有寫入工具 |
| `guards` | **唯讀報表**:掃一個目錄底下所有 config,配上 tmux 裡活著的守護 agent,一條 pipeline 印一行。吃 `--dir`(預設當前目錄),不吃 `--config` |
| `retry --milestone N` | 人工處理完卡住的 PR 後,把 review 輪數歸零讓迴圈可以再跑,**保留** PR 編號與 implementer 的 session_id(不丟 context) |
| `reset --milestone N` | 整個 milestone 的進度清掉重來(PR、session 都不留);不加 `--milestone` 則清除全部 |

守護 agent 多半跑在另一台機器上,所以 `guards` 的典型用法是一行 ssh:

```bash
ssh mac 'cd ~/Documents/agents-dev-tools && .venv/bin/python -m milestone_pipeline guards'
```

```
guard-formosa   formosa.yaml      M6 review  round=3  ~$21.40  2 分鐘前
guard-shopapp   shopapp.yaml      M2 await_human  (merge_gate)  41 分鐘前  ⚠
    放行: python -m milestone_pipeline approve --milestone 2 --config shopapp.yaml
    打回: python -m milestone_pipeline reject --milestone 2 --reason "..." --config shopapp.yaml
    看它: tmux attach -r -t guard-shopapp
(無 guard)      sideproj.yaml     M4 stuck  3 小時前  ⚠ 沒有守護 agent 在顧
```

⚠ 只給「停下來等人」,**不給「多久沒動」** —— implement 階段跑一小時不寫存檔是
正常的,時間照印但不拿來判斷。同理沒有守護 agent 本身不是問題(`nohup … run`
就是這樣跑的),只有「停下來等人**而且**沒有守護 agent 會去按」才會被點名。

Plan file 格式見 `plan.example.md`:每個 `## ` 標題一個 milestone,
第一個標題前的內容是整體背景(每個 milestone 都會附給 implementer)。

## 狀態機

每個 milestone 各自有一個 phase,存在 `.pipeline-state.json`:

```
implement ──實作完成、PR 開好──▶ review ──APPROVE──▶ merge ──▶ done
                                   │
                                   └──輪數用完──▶ stuck ──(人工處理 + retry)──▶ review
```

存檔刻意極簡:phase、PR 編號、branch、implementer 的 session_id、
review 輪數、兩邊的花費。任何一步 crash 都可以重跑 `run` 接回去。

## 安全與成本開關

- **工具邊界**:SDK 的 `allowed_tools` 只是「免詢問」清單,**不會**限制工具
  存在與否 —— 真正的限制要靠 `tools`。所以 reviewer 的 `tools` 只給
  `Read/Glob/Grep/Bash`(Bash 是為了跑 `gh`),拿不到 `Edit`/`Write`。
- `implementer.permission_mode`:`acceptEdits`(預設)自動核准檔案編輯;
  `bypassPermissions` 全自動但建議只在隔離環境(container/VM)使用。
- `reviewer.permission_mode`:預設 `default`。reviewer 沒有寫入工具,
  不需要(也不應該)設成 `acceptEdits`。
- `max_budget_usd`:implementer 以「每個 milestone 的 session」計,
  reviewer 以「每輪」計,超過即中止該次呼叫。超支或用完 `max_turns` 時
  agent 會回報錯誤,orchestrator 會中止流程而不是默默繼續下一輪。
- `loop.max_review_rounds`:防止兩個 agent 無限對話;超過上限會在
  PR 留言並停下等人工介入(`status` 會顯示 `stuck`,exit code 1)。

## 開發

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

測試都是不碰網路 / `gh` / SDK 呼叫的純邏輯測試(plan 解析、verdict 解析、
state 讀寫、config 驗證、prompt 的 remote 處理)。不過 `reviewer.py` 在
module level import `claude_agent_sdk`,所以仍需要安裝該套件才能收集測試。

## 已知簡化(骨架階段)

- verdict 靠 reviewer 輸出**自成一行**的 `VERDICT: APPROVE|REQUEST_CHANGES`
  解析(取最後一個);解析失敗時保守視為要求修改。
- **reviewer 與 implementer 共用同一組 `gh` 認證**,所以 GitHub 會拒絕
  reviewer approve 自己帳號開的 PR(`Can not approve your own pull request`)。
  prompt 已指示遇到這個錯誤時改用 `gh pr comment` 留下同樣的意見全文,
  流程不受影響(verdict 走 stdout 解析)。要讓 review 真的顯示在 PR 的
  review 狀態上,需要另一個 GitHub 帳號 / bot token。
- reviewer 的行內 comment 與 fixer 的 thread 回覆用 `gh pr comment`
  簡化處理;要做到逐 thread 回覆可改用 `gh api` 的 review threads API。
- 未處理 merge conflict(後面的 milestone 疊在最新 base 上,
  正常序列執行時不會發生;人工介入過的 PR 需自行 rebase)。
- 混合版(`reviewer.type: actions`)需要你在 repo 裝好
  `on: pull_request` 觸發的 claude-code-action review workflow,
  這裡只實作了輪詢端。

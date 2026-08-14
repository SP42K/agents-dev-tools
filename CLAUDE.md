# CLAUDE.md

`milestone_pipeline` 是一個 orchestrator:讀 plan file,讓 implementer agent
逐個 milestone 實作並開 PR,讓 reviewer agent review,兩者來回直到 approve,
最後由 orchestrator 自己 merge。跑在 `claude-agent-sdk` 之上。

## 指令

```bash
pip install -e ".[dev]"     # 測試需要 claude-agent-sdk(reviewer.py 在 module level import)
pytest                      # 全部測試,不碰網路 / gh / SDK 呼叫
pytest tests/test_pipeline.py::test_parse_verdict -q
```

專案本身的 CLI(注意 `--config` 是必要的,預設值 `pipeline.yaml` 裡的
`repo.path` 是佔位字串):

```bash
python -m milestone_pipeline status --config my-pipeline.yaml
```

## 職責分界

**確定性的事一律留在 code 裡,不要交給 prompt 自律。** 這是這個 repo 的核心
設計約束:找 PR、數輪數、merge、存進度、判斷是否超支 —— 都由 orchestrator 的
Python 決定。agent 只負責需要智慧的部分(實作、review、修復、回覆)。

新增流程控制時先問:這件事能不能用 code 保證?能的話就不要寫進 prompt。

## 非顯而易見的陷阱

**`tools` 才是工具白名單,`allowed_tools` 不是。** SDK 的
`allowed_tools` 只是「這些工具免詢問」,**不會**限制工具存不存在。要真正
拿掉 reviewer 的 `Edit`/`Write`,必須設 `tools=`。兩個 agent 的清單分別是
`implementer.IMPLEMENTER_TOOLS` 和 `reviewer.REVIEWER_TOOLS`,兩個常數同時
餵給 `tools` 和 `allowed_tools`。改動任一邊之前先確認你要改的是哪個語意。

**兩邊的花費語意不同,不要混用。** SDK 每次回傳的 `total_cost_usd` 是**該
session 的累計值**。implementer 是一個 milestone 一個持久 session,所以
`implementer_cost_usd` 要**覆寫**;reviewer 每輪都是 fresh session,所以
`reviewer_cost_usd` 要**累加**。`MilestoneState.cost_usd` 是兩者相加的
property(不是 dataclass field,所以不會進存檔)。

**agent 的錯誤要自己檢查,SDK 不會丟例外。** `max_turns` / `max_budget_usd`
用完、或 API 層 429/5xx,都是正常回傳一個 `ResultMessage`。`runner.py` 同時
看 `is_error` 和 `subtype`(API 失敗時 `is_error=True` 但 `subtype` 仍是
`"success"`,只看其中一個會漏)。任何新增的 agent 呼叫都要檢查
`result.is_error`,否則流程會默默帶著壞結果往下走。

**verdict 是 `prompts.py` 與 `reviewer.py` 之間的隱性契約。**
`review_prompt` 要求最後一行輸出 `VERDICT: APPROVE|REQUEST_CHANGES`,
`reviewer._VERDICT_RE` 只認**自成一行**的形式並取最後一個。改其中一邊
一定要同步改另一邊,並更新 `test_parse_verdict*`。解析不到時保守回
`False`(視為要求修改),這是刻意的 —— 不要改成 fail-open。

**reviewer 無法 approve 自己帳號開的 PR。** implementer 與 reviewer 共用
同一組 `gh` 認證,GitHub 會回 `Can not approve your own pull request`。
prompt 已指示改用 `gh pr comment` 並繼續;流程不依賴 PR 的 review 狀態,
verdict 走 stdout 解析。

**`retry` 與 `reset` 不一樣。** `retry --milestone N` 把輪數歸零但**保留**
PR 編號與 session_id(人工修完卡住的 PR 之後用);`reset --milestone N` 是
整個清掉重來。改動 stuck 相關邏輯時不要把兩者合併。

## 慣例

- 註解與 docstring 用繁體中文,識別字用英文。
- 每個模組單一職責:`config`(載入+驗證)、`plan`(解析)、`state`(存檔)、
  `gh`(subprocess 封裝)、`prompts`(所有模板)、`runner`(SDK 回應收集)、
  `implementer`/`reviewer`(agent 封裝)、`orchestrator`(主迴圈)。
  prompt 文字一律放 `prompts.py`,不要散在 agent 模組裡。
- `config.py` 的列舉值(`permission_mode` / `merge_method` / `reviewer.type`)
  在載入時就驗證並丟 `SystemExit`,不要延後到執行期才炸。
- `state.py` 載入時會濾掉不認得的 key,所以刪欄位是相容的;**加**必填欄位
  不是(舊存檔會拿到 dataclass 預設值)。加欄位請給預設值。
- 流程中止用 `orchestrator.PipelineError`,`__main__` 會轉成 exit code 1。

## 測試

`tests/test_pipeline.py` 全部是純邏輯測試,不 mock SDK、不碰網路。碰到
需要 mock `ClaudeSDKClient` 才能測的東西,通常代表那段邏輯該從 agent 模組
抽到可獨立測試的地方。

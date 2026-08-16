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
現在有兩個模板要對這個 regex 負責(`review_prompt` 與 `hybrid_review_prompt`),
所以結尾那段文字抽成 `prompts._VERDICT_INSTRUCTION` 共用;新增模板請沿用它,
不要各寫一份。

**reviewer 無法 approve 自己帳號開的 PR。** implementer 與 reviewer 共用
同一組 `gh` 認證,GitHub 會回 `Can not approve your own pull request`。
prompt 已指示改用 `gh pr comment` 並繼續;流程不依賴 PR 的 review 狀態,
verdict 走 stdout 解析。

**`max_review_rounds` 是安全網,不是收斂機制。** reviewer 每輪都是 fresh
session,所以第 3 輪的 reviewer 會用全新的眼睛掃同一份 diff,自然挑出**另一組**
nit —— PR 越來越好卻永遠不收斂,調高上限只是在餵這個迴圈。真正的收斂在
`prompts.round_notes()`:reviewer 掃過一次之後插入 `_SCOPE_LOCK`(新發現只有 bug / 資安 /
規格沒做到 / 測試沒過才能影響 VERDICT,其餘一律寫成「後續建議」),最後一輪
再插入 `_FINAL_ROUND`(明講這輪 REQUEST_CHANGES 就會停下來等人,所以只擋
blocker)。「第幾輪、是不是最後一輪」由 orchestrator 算好傳進去(`is_final`),
agent 只負責判斷 —— 同 verify gate,不新增 phase / 狀態 / 契約。
**兩個 review 模板都要吃到 `round_notes`**,只改一個等於 hybrid 模式沒有收斂
機制。`ActionsReviewer` 收得到 `is_final` 但只能忽略(它的 prompt 在對方 repo
的 workflow 裡)。

blocker 清單的**第五項(對外承諾與實際行為不符:tool 描述、回應的 notes、
錯誤訊息、使用者文件)不能拿掉**。實測(formosa M6)最有價值的三個發現都是
這個形狀,而且 fixtures 測不出來、typecheck 也過,只有 reviewer 逐字對得出來。
少了這項,`_SCOPE_LOCK` 會把它降級成「後續建議」——而寫在已 merge 的 PR 上的
後續建議實務上等於沒人會做。

**`_SCOPE_LOCK` 看 `ms.reviewer_seen`,不是 `round_no > 1`。** 輪數不是「reviewer
掃過幾次」的代理:人工 `reject` 的那一輪會 `review_round += 1` 但**跳過 reviewer**
(意見直接交給 implementer),所以 reviewer 一出場就是 round 2 —— 用輪數的話它
一出場就被鎖在「人的意見」那個範圍裡,而 `reject` 的典型用法是「接續你被中斷的
修復」,implementer 在那一輪往往改了遠超出人意見範圍的東西,這份 PR 於是**從頭到尾
沒拿到過一次不受限的完整掃描**。不變式:**人已接手、PR 即將大幅改變時就要清掉
`reviewer_seen`** —— 先前那次掃描涵蓋不到接下來的東西。目前那等於 `retry` 與
`reject`(兩者也都會把 `review_round` 歸零),但**要清的理由是前者,不是「輪數
歸零」這個表象** —— 拿輪數當代理,正是這一段一開始踩的那顆雷。
旗標只在 reviewer 回傳且 `is_error` 為假之後才設 True;
出錯那次可能只讀了半份 diff。退化方向是安全的:False 只是多一次完整掃描。
(verify 失敗**不會**有這個問題 —— 它發生在 reviewer 已經跑過並 APPROVE 的**同一輪**
內,只是換掉 `feedback`,不會多出一個沒有 reviewer 的輪次。)

**`is_final` 綁著 `merge_gate`,不是單看輪數。** `_FINAL_ROUND` 的語意是
「非 blocker 一律放行」,所以 `merge_gate: auto` 下它等於**輪數用完自動 merge**
—— 把 `PH_STUCK` 這道 fail-closed 的安全網換成 fail-open,方向與 `VERDICT` 的
doctrine 相反。因此 `is_final` 要同時滿足「輪數到頂」與
`loop.needs_human_merge(m.index)`,`auto` 時照舊落到 `PH_STUCK` 等人。
代價很小:`_FINAL_ROUND` 本來就只在最後一輪觸發,真正省輪數的是每輪都在的
`_SCOPE_LOCK`。新增任何「放寬 reviewer 標準」的機制之前,先問**放寬之後誰還在
把關**。

**`UNRESOLVED` 是第二個 agent↔code 契約,但方向與 `VERDICT` 相反。**
`fix_prompt` 要求 implementer 最後一行輸出 `UNRESOLVED: YES|NO`,表示它與
reviewer 有沒有沒談攏的爭點;有的話 orchestrator 停下來讓人裁決。
**解析不到時回 `False`(fail-open),這與 `VERDICT` 的 fail-closed 是刻意相反的**
—— 下游還有 reviewer 的 APPROVE 擋著,而 agent 漏掉結尾標記很常見,
若這裡也保守處理,無人值守跑會每輪都停。呼叫端用
`has_unresolved_marker()` 分辨「沒說」與「說了 NO」並記警告。
契約的模板與 regex **都在 `prompts.py`**(有別於 `VERDICT` 散在兩個模組),
新增契約請沿用這個作法。

**`reviewer.type: hybrid` 只能用 `ocr delegate`,不要改成 `ocr review`。**
`ocr`(alibaba/open-code-review)有兩種模式,差別是誰出 LLM:`ocr review`
要它自己的 API key(`OCR_LLM_*` / `ANTHROPIC_BASE_URL` 那組),**訂閱制的
Claude 認證餵不進去**;`ocr delegate` 標榜 `no LLM required`,只做確定性的
檔案篩選與規則比對,判斷全部由宿主 agent(這裡就是 reviewer 的 Claude
session)做。走 delegate 才能只靠一組 Claude 認證跑完,也不會有第二筆帳單。

**OCR 是輔助,不是關卡。** delegate 模式下它連程式碼都沒讀過,產出只有
「該審哪些檔」+「每個檔套哪組檢查項目」,verdict 仍然只由 Claude 下。
它給的規則開頭寫著 `Favor precision over recall`,那是它自己的取捨;
`hybrid_review_prompt` 必須明講**檔案清單是下限、規則是提醒**,否則
reviewer 會跟著收斂,而它是 merge 前唯一的關卡。它也**不跑測試、不看
milestone 驗收條件**,還會用 `unsupported_ext` 略過 `.md` 等檔案 ——
被略過的檔仍在 diff 裡,prompt 一定要把它們點名回來。

**這段刻意 fail-open,方向與 `VERDICT` 相反。** OCR 沒裝、逾時、JSON 壞掉,
`HybridReviewer._scan()` 一律**只記警告並把失敗原因寫進 prompt**,不設
`is_error`、不 park —— 理由同 `UNRESOLVED`:下游還有 `VERDICT` 這道
fail-closed 關卡,若這裡 fail-closed,一台沒裝 `ocr` 的機器會每輪 review 都
停下來等人。失敗原因**必須**同時進 log 與 prompt
(`prompts.ocr_unavailable_note`),不然「OCR 長期悄悄沒在跑」不會有人發現。
同理,`format_review_plan` 的規則截斷也一定要在文字裡註明。

**Windows 上呼叫 npm 裝的 CLI 一定要先 `shutil.which()`。** npm 裝出來的是
`ocr.CMD`,而 `subprocess` 不做 PATHEXT 解析,直接傳 `"ocr"` 會
`FileNotFoundError` —— 明明 shell 裡跑得動。見 `ocr.Ocr._resolve_exe()`。

**verify gate 是唯一的確定性驗收,但它刻意不新增任何流程狀態。**
`loop.verify_command` 在 reviewer `APPROVE` **之後**跑(`orchestrator._verify()`),
失敗時把輸出包成這一輪的意見交回 implementer(`prompts.verify_fail_prompt` →
既有的 `fix_prompt`),phase 維持 `PH_REVIEW`。刻意**不**新增 phase、不新增
`R_*` 通知理由、不新增 agent↔code 契約 —— 它共用既有的 `max_review_rounds`,
輪數用完就落到既有的 `PH_STUCK`。三個容易漏的點:

- **一定要先 `gh.checkout(ms.branch)`。** `checkout_base()` 只在 `PH_IMPLEMENT`
  開頭跑過,crash 後從 `PH_REVIEW` resume 時 repo 可能停在任何分支,
  不切就會在錯的分支上驗收。
- **`approve` 會繞過 verify**(它直接把 phase 設成 `PH_MERGE`)。這是刻意的
  ——人已經裁決過了 —— 但要重跑驗收就得用 `reject`。
- **指紋只在失敗時存**,所以 `last_verify_fingerprint` 有值就等於「上次失敗過」。
  `workspace_fingerprint()` 取不到時回 `""` → 照跑(退化方向安全)。
  它同時看 `rev-parse HEAD` / `status --porcelain` / `diff HEAD`,第三個不能省:
  少了它,agent 再改一次「本來就已修改」的檔案不會改變狀態碼,指紋不變 →
  被誤判成沒動作而跳過驗收。

**SDK 只能從 `backend.py` 進來。** `implementer.py` / `reviewer.py` 現在都透過
`AgentBackend`(持久 session + 一次性 query,回傳一律是 `AgentResult`)。
`runner.py` 仍直接 import `AssistantMessage` / `TextBlock` —— 那是**訊息解析**,
不是 backend 選擇,不要順手搬。新增 backend 要同步加進 `config.BACKENDS`
(載入時就 `_one_of` 驗)。**不要順手實作第二個 backend**:評估過的替代品沒有
工具白名單也沒有權限模式,`REVIEWER_TOOLS` 靠 `tools=` 拿掉 `Edit`/`Write` 的
**保證**會退化成 prompt 自律。

**agent 的文字靠 `runner.log_text()` 即時落地。** `collect_response(..., on_text=)`
是純旁路,不影響 `AgentResult`。`implementer.ask` 與 `ScriptReviewer.ask_claude`
有接,`compact()` 刻意沒接(雜訊)。**不要把 `on_text` 預設成寫 stdout** ——
無人值守跑時 handler 由 `__main__` 決定。

**merge gate 一定要配 `merge_approved` 旗標。** `approve` 之後 phase 回到
`PH_MERGE`,如果不記「人已放行過」,`needs_human_merge()` 會再次成立 →
park → approve → park 無限迴圈。任何新增的關卡都要想清楚「放行後
憑什麼不再次觸發」。

**守護 agent 不能有寫入工具 —— 這是 code,不是 prompt。** `guard.py` 起的那個
session 是 park 點上的「人」,它唯一的產出應該是 `approve` / `reject`。
`_DENY` 分兩段:`Edit` / `Write` / `NotebookEdit` 擋掉產生檔案,
`git commit` / `git push` / `gh pr merge` 擋掉讓檔案生效 —— **兩段都要**,
因為前三個擋不住 Bash 裡的 `cat > file`。實測(formosa M8):只靠 prompt 時,
守護 agent 在 merge gate 上算出「一行的事直接修比 reject 省 10 分鐘」,
commit 進分支,那個 commit 沒經過 reviewer、沒經過 verify,merge 時 CI 還在跑。
它讀得懂規矩,規矩當時只是文字。**新增守護 agent 的能力之前,先問這個能力能不能
被用來繞過 reviewer 或 verify。**

**守護 agent 要 tmux,orchestrator 不用 —— 兩者相反,不要抄錯。** orchestrator
從不讀 stdin,`nohup … &` 就夠;守護 agent 是**互動 session**,而 unsnooze 的
喚醒方式是往它的 pane 裡打字,沒有 pane 就沒有自動恢復。所以 `guard` 有 tmux
就起在 detached session 裡。session 名字(`guard-<config stem>`)同時是身分、
存活證明、與 attach 入口 —— **不要為了「有沒有守護 agent 在跑」另外發明 pid 檔**,
`tmux has-session` 就是答案,而且 session 消失不留殘骸。

**起飛前的檢查看「乾不乾淨」,不看「在哪個分支」。** `guard.repo_warnings()`
警告未 commit 的變動與落後 upstream,**刻意不檢查在不在 `master` 上** ——
在 feature branch 上開發這條 pipeline 是正常的(`guard` 自己就是),
分支名不是「跑的 code 是不是 git 裡那份」的代理。同 `_SCOPE_LOCK` 用輪數
代理 `reviewer_seen` 那顆雷。而且只警告不擋:起飛路徑上不該有互動問題
(同 park & notify 的理由),要選擇就加旗標,不要加提問。

**同一份存檔只能有一個 `run`,而且鎖檔要進 `_own_artifacts()`。**
`lock.exclusive()` 用 OS 檔案鎖(不是 pid 檔:crash / `kill -9` 之後 OS 自己
放掉,不留要人判斷的殘骸)。鎖檔跟 state 檔同一個目錄,而 state 檔多半就在
**目標 repo 裡** —— 它是未追蹤的新檔案,不排掉的話除了讓 `workspace_fingerprint`
每次都不一樣,連 reviewer 與 merge gate 的「`git status` 乾淨」都會被它汙染。
任何新增的自家產物都要問一次:它會不會出現在目標 repo 的 `git status` 裡?

**`unsnooze` 只能用 launcher 包法,不要 `unsnooze install`。** 後者裝的是全域
hook,對機器上每個 claude session 生效;`unsnooze [claude args...]` 天生只包住
守護 agent 自己(需求就是「只對守護 agent 生效」)。`guard.run()` 偵測到全域
hook 會警告但不擋 —— 同 OCR 那段的 fail-open 理由:這是範圍問題,不是正確性問題。
Windows 上 unsnooze 跑不起來(靠 tmux / Zellij 的 pane 恢復),降級成印警告後
照常跑,**deny 與 prompt 兩邊仍然生效** —— 別為了 auto-resume 去弱化那兩個。

**四個人工介入指令語意都不同,不要合併。**
`retry` 卡住(輪數用盡)後重置輪數,保留 PR 與 session;
`approve` 放行停在決策點的 milestone;
`reject` 打回,把 `--reason` 當成下一輪的 review 意見**直接交給 implementer
並跳過 reviewer**(同時把輪數歸零,因為人已接手);
`reset` 整個清掉重來。

## 慣例

- 註解與 docstring 用繁體中文,識別字用英文。
- 每個模組單一職責:`config`(載入+驗證)、`plan`(解析)、`state`(存檔)、
  `gh` / `ocr` / `verify`(各自一個外部命令的 subprocess 封裝)、
  `prompts`(所有模板)、`runner`(SDK 回應收集)、`backend`(SDK 呼叫)、
  `implementer`/`reviewer`(agent 封裝)、`notify`(決策通知)、
  `guard`(守護 agent 的啟動與權限邊界)、`lock`(存檔的獨佔鎖)、
  `orchestrator`(主迴圈)。
  prompt 文字一律放 `prompts.py`,不要散在 agent 模組裡。
- **通知失敗永遠不能中斷 pipeline。** `MultiNotifier` 會吞掉每個 channel 的
  例外並記 log:狀態早就存檔了,人就算沒收到推播,`status` 也看得到。
  新增 channel 時不要在外層再往上拋。
- 停下來等人時走 **park & notify**(存檔 → 通知 → exit 1),不要在
  orchestrator 裡 `input()` 等人 —— 那會讓無人值守跑不起來,且 crash 就前功盡棄。
- `config.py` 的列舉值(`permission_mode` / `merge_method` / `reviewer.type`)
  在載入時就驗證並丟 `SystemExit`,不要延後到執行期才炸。
- `state.py` 載入時會濾掉不認得的 key,所以刪欄位是相容的;**加**必填欄位
  不是(舊存檔會拿到 dataclass 預設值)。加欄位請給預設值。
- 流程中止用 `orchestrator.PipelineError`,`__main__` 會轉成 exit code 1。

## 測試

`tests/test_pipeline.py` 全部是純邏輯測試,不 mock SDK、不碰網路。碰到
需要 mock `ClaudeSDKClient` 才能測的東西,通常代表那段邏輯該從 agent 模組
抽到可獨立測試的地方。

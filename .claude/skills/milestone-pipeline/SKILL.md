---
name: milestone-pipeline
description: >
  用 agents-dev-tools 的 milestone_pipeline 驅動另一個 repo 的開發:把計畫書
  轉成 orchestrator 吃得下的 plan file、寫 config、起飛前檢查、跑無人值守的
  implement → PR → review ↔ fix → merge 迴圈,以及停在決策點時該用
  approve / reject / retry / reset 的哪一個。當使用者想「用 pipeline 跑某個
  專案」「把 plan.md 交給 agent 實作」「pipeline 停住了怎麼辦」「evaluate 某個
  repo 能不能用這套跑」時使用。包含 crash 後的續跑、merge gate 該由人驗什麼、
  以及實測出來的預算/輪數基準值。
---

# 用 milestone_pipeline 驅動一個專案的開發

`milestone_pipeline` 是 orchestrator:讀 plan file,讓 implementer agent 逐個
milestone 實作並開 PR,reviewer agent review,兩者來回到 approve,最後由
orchestrator 自己 merge。**確定性的事(找 PR、數輪數、merge、存檔、判斷該不該
停下來問人)都在 Python 裡,agent 只負責需要智慧的部分。**

下面的內容是實際跑完一個 TypeScript monorepo(formosa-mcp,6 個里程碑)累積的,
不是從 README 推導的。

## 0. 先判斷這個專案適不適合

跑之前先確認三件事,任何一項不成立就先解決,不要硬上:

- **驗收條件 agent 驗得了嗎?** 需要真實 API 金鑰、實際部署、或人的主觀判斷的
  milestone,agent 會「宣稱完成」而 reviewer 只看 diff 抓不到。這種要配
  `merge_gate: ask`,由人在 merge 前驗。
- **有沒有 `CLAUDE.md`?** 這是投報率最高的一項,見 §2。
- **`gh` / ssh / 金鑰備齊了嗎?** 見 §4 起飛前檢查。

## 1. 把計畫書轉成 plan file(最容易出錯的一步)

**不要直接把產品計畫書餵給 orchestrator。** `plan.py` 是用 `^##\s+` 切檔案,
每一段就是一個 milestone。一般的計畫書會被切成一堆假 milestone(「1. 一句話定位」
「2. 範圍」…),而真正的里程碑常常躲在某個表格的**列**裡,根本不是標題。

正確做法是另寫一份執行計畫(例如 `docs/pipeline-plan.md`),規則如下。

### 結構

```markdown
# 專案名 — 執行計畫

這段文字在第一個 `## ` 之前,是 **preamble**,每個 milestone 都會拿到全文。

### 開工前必讀(每個 milestone 都適用)

> 用 `###` 不是 `##` 是刻意的 —— `##` 會被當成新的 milestone。

金鑰紀律、通用工程約定、不要碰哪些檔案 —— 放這裡。

## Milestone 1: Ascii Title Here

這段是 milestone 1 的 body,只有做這個 milestone 的 agent 看得到。

## Milestone 2: Another Ascii Title
```

### 六條硬規則

1. **只有 `##` 開 milestone,子標題一律用 `###` 以上。** `###` 安全,因為
   `^##\s+` 要求 `##` 後面接空白。程式碼區塊裡也不要出現行首的 `## `。
2. **preamble = 第一個 `##` 之前的全部內容**,每個 milestone 都拿得到。
   跨 milestone 的紀律(金鑰、測試規矩、別碰狀態檔)寫在這裡,不要每段重複。
3. **標題用 ASCII。** branch 名從標題產生(`milestone/<index>-<slug>`),
   中文標題會生出中文 branch 名 —— 在 cp950 預設的 Windows shell 上是自找麻煩。
   內文照 repo 慣例用中文沒問題。
4. **`Milestone N: ` 這個前綴會被自動剝掉**(regex `^milestone\s*\d+\s*[:：]\s*`),
   所以標題寫 `## Milestone 1: CWA weather adapter`,`status` 顯示的是
   `CWA weather adapter`,branch 是 `milestone/1-cwa-weather-adapter`。
5. **index 是解析順序,不是原計畫的週次。** 已經做完的里程碑**不要列進去**,
   從還沒做的開始編號 1。
6. **body 要自給自足,但不要抄 CLAUDE.md。** implementer 每個 milestone 都是新
   session,不會自己去翻計畫書第幾節;但它讀得到 `CLAUDE.md` 與其指向的文件,
   所以正確做法是「指路 + 寫出這個 milestone 專屬的規格」,而不是複製既有文件。

### 驗收條件怎麼寫

寫成 **agent 驗得了的形式**,人才驗得了的部分留給 merge gate:

```markdown
驗收條件(全部達成才算完成):

1. `npm run lint`、`npm run typecheck`、`npm test` 全綠。
2. 每個 tool 各有測試,用 fixtures 跑,不打真實 API。
3. fixtures 取材自真實回應:PR body 說明每個 fixture 對應哪一次真實請求。
4. PR body 附上實測基準表(回應大小、耗時、有沒有分頁上限、rate limit 行為)。
5. 開一份 docs/<milestone>-notes.md 記這個 milestone 專屬的決策與坑。
```

**驗收例子一定要是現實中成立的。** 實際踩過:計畫書寫「307 到民生社區還要幾分鐘?」,
但 307 根本不經過民生社區 —— 照著驗會得到 ToolError,看起來像 tool 壞了,
其實 tool 完全正常。編出來的例子會浪費一整輪。

### 該寫進 body 的其他東西

- **上游 rate limit**(例如「免費額度 5 次/分鐘」)。不寫的話 agent 會用迴圈掃,
  把額度打爆然後卡住自己的驗證。
- **明確排除的範圍**:部署、發佈、登錄 registry 這些需要帳號與判斷的事,
  改成要求交付「教學文件 + 人還要做什麼的清單」。
- **架構約束**:例如「載入 .env 是進入點的責任,不能寫進共用的 buildServer」——
  這類決定 agent 不會自己想到,但事後改很貴。

### 寫完一定要驗

```bash
python -c "
from pathlib import Path
from milestone_pipeline.plan import Plan
p = Plan.load(Path('/path/to/target/docs/pipeline-plan.md'))
print('milestones:', len(p.milestones), '| preamble:', len(p.preamble))
for m in p.milestones: print(' ', m.index, m.branch, len(m.body))
"
```

數量對不上、branch 名出現非 ASCII、或 preamble 短得可疑 —— 都是格式寫錯了。
**實際踩過:把「開工前必讀」寫成 `##`,它就變成了 milestone 1。**

## 2. 準備目標 repo

**`CLAUDE.md` 是投報率最高的一件事。** implementer 每個 milestone 都是新 session,
reviewer 每輪都是新 session,兩邊都讀得到 `CLAUDE.md`。把「build 與 test 的因果」
「反覆被違反的約定」「靜默失敗的地雷」寫進去,比調 pipeline 的 prompt 有效得多 ——
它同時提升實作品質與 review 品質。

已驗證:即使 `system_prompt` 傳的是純字串(會取代 Claude Code 預設 prompt),
`CLAUDE.md` 仍然會被注入(`setting_sources` 預設 `None` = 全載入)。

其他:

- **把狀態檔加進 `.gitignore`**(預設 `.pipeline-state.json`,寫在 repo 根目錄)。
  不擋的話 implementer 一個 `git add -A` 就把它 commit 進 PR。
- **金鑰放 `.env` 並確認真的有人載入它。** 「`.env` 在 gitignore 裡」不等於
  「程式會讀 `.env`」—— 實際踩過:repo 只是 gitignore 了它,沒有任何東西載入。
- **確認 `base_branch`**(不一定是 `main`)與**既有的 merge 慣例**
  (`gh pr list --state merged` 看是 squash 還是 merge commit)。

## 3. 寫 config

```yaml
repo:
  path: C:/Users/you/Documents/target-repo
  base_branch: master            # 確認過,不要假設是 main
  remote: origin

plan:
  path: docs/pipeline-plan.md    # 不是產品計畫書

implementer:
  model: opus
  permission_mode: acceptEdits
  max_turns: 200                 # 見下方實測值
  max_budget_usd: 40.0

reviewer:
  type: script
  model: opus
  permission_mode: default       # 唯讀工具,不需要 acceptEdits
  max_turns: 40
  max_budget_usd: 5.0

loop:
  max_review_rounds: 5
  compact_between_rounds: true
  merge_method: merge            # 對齊 repo 既有慣例
  merge_gate: ask                # 見 §6
  merge_gate_from_milestone: 1
  gate_on_unresolved: true
  gate_on_agent_error: true

notify:
  channels: [desktop, pr_comment]   # park 之後會 exit,沒通知不會知道
  mention: "@your-github-handle"

state_file: .pipeline-state.json
```

### 實測基準值(opus,中型 milestone)

| 項目 | 實測 |
|---|---|
| 一個 milestone 的實作階段 | 20–40 分鐘,$10–15 |
| 六~七個 tool + 測試 + fixtures 的規模 | 40+ 檔案、4000–7000 行 |
| `max_turns: 120` | **不夠** —— opus 把程式全寫完了,卡在 commit / push / 開 PR 之前 |
| 一輪 review(40+ 檔案的 diff) | $2–3 |
| `reviewer.max_budget_usd: 3.0` | 太薄,第一輪就用掉 $2.05 |

`max_turns` 用盡與預算用盡都是 `park`(有 `gate_on_agent_error: true`),
不是崩潰 —— 但每次都要人介入很吵,寧可一開始就給夠。

## 4. 起飛前檢查

```bash
# 缺任何一項都會在半路才炸
claude --version          # SDK 要靠它起 agent
gh --version && gh auth status
node --version            # 目標專案需要的話
python -c "import claude_agent_sdk; print('sdk ok')"
```

**`gh` 一定要在 PATH 上。** orchestrator 的 `gh.py` 用
`subprocess.run(["gh", ...])`,implementer 要 `gh pr create`,reviewer 要
`gh pr diff` —— 全靠 PATH 解析。失敗症狀很難認:agent 實作完、push 完,
然後 orchestrator 丟「找不到 open PR」,一個 milestone 的預算就白燒了。
Windows 上 gh 可能裝在 `%LOCALAPPDATA%\Programs\gh\bin`(不是 `GitHub CLI\`)。

其他:remote 是 SSH 的話要有 ssh-agent(orchestrator 自己會 `git fetch`/`pull`);
目標 repo 的 base branch 要乾淨;`.env` 金鑰填好。

## 5. 跑

```bash
# 長時間跑,丟背景並導向 log
python -m milestone_pipeline run --config target.yaml >> /tmp/pipeline.log 2>&1

python -m milestone_pipeline status --config target.yaml   # 看進度
```

exit code:`0` = 全部完成;`1` = 停下來等人或流程錯誤。

**orchestrator 的 log 很稀疏**(只在階段轉換時寫),而 agent 的中間過程收在
記憶體裡不會即時落地。要看它在做什麼,去看目標 repo 的 git 痕跡:

```bash
git branch --show-current      # branch 建了沒
git log --oneline master..HEAD # commit 了沒
git status --short             # 正在寫哪些檔案
```

分支建了、工作區還乾淨 = 還在探索/讀文件/打真實 API 探路,是正常的。

## 6. 停下來時:四個指令語意完全不同

| 指令 | 語意 |
|---|---|
| `approve` | 放行停在決策點的 milestone,回到中斷前的 phase 繼續 |
| `reject --reason "..."` | 把 reason 當成**下一輪的 review 意見直接交給 implementer 並跳過 reviewer**,輪數歸零 |
| `retry` | 輪數用盡卡住後重置輪數,保留 PR 與 session |
| `reset` | 整個清掉重來 |

### `reject` 才是「接續中斷的工作」的正確工具

名字有誤導。當 implementer 修到一半被預算/輪數切斷時:

- `approve` → 回到 review 階段、輪數 +1 → **再叫一次 reviewer**(多花一輪錢),
  而它看到的 diff 還沒有那些未 commit 的修改,大概率把同樣的意見再講一遍。
- `reject --reason "接續你被中斷的修復,意見全文在 PR #N 的 comment 裡"` →
  跳過 reviewer,直接讓 resume 的 session 接著做。**這才是對的。**

### park 的原因與對策

| `await_reason` | 意思 | 對策 |
|---|---|---|
| `merge_gate` | reviewer approve 了,等人放行 | 見 §7,驗完再 `approve` |
| `agent_error` | 超預算 / 超輪數 / API 失敗 | 先看 `status` 的 subtype:`error_max_budget_usd` 要加預算,`error_max_turns` 要加輪數 —— **加完再放行,否則會立刻再撞一次** |
| `unresolved` | implementer 自陳與 reviewer 有爭點 | 人裁決,`approve` 接受現狀或 `reject` 給指示 |
| `stuck` | review 輪數用盡仍未 approve | 人工處理 PR 後 `retry` |

`error_max_budget_usd` 有個陷阱:resume 的是同一個 session,SDK 回報的
`total_cost_usd` 是累計值,所以續跑前**一定要把上限拉高**。

## 7. merge gate:人該驗什麼

**這是整條 pipeline 唯一無法自動化的環節,不要當形式。**

reviewer 會把 fixtures 驗到極致(本機重跑 lint/typecheck/test、逐條對 diff),
但它**只看得到程式碼與 fixtures**。要打真實 API、要實際部署才看得見的問題,
只有人驗得到。實際抓到過的:

- 一個地點解析的 bug,錯誤訊息說「沒有『市』這個行政區」,而同一則訊息的 hint
  第一個就列出那個行政區 —— 自己打自己的臉,但 fixtures 測不出來。
- 一個縣市沒有上游資料集,回的是原始 400「參數錯誤」,而使用者根本沒給過縣市
  (是程式從座標推的),等於給了一個無從執行的建議。

驗的方法是寫一支一次性腳本,直接組 context 呼叫 handler(繞過 MCP 協定):

```js
import "dotenv/config";
import { createMemoryCache, fetchJson, fetchText, fetchBuffer } from "@scope/core";
import { theAdapter } from "@scope/adapter-x";
const ctx = { cache: createMemoryCache(), fetchJson, fetchText, fetchBuffer, env: process.env };
const tool = theAdapter.tools.find((t) => t.name === "get_something");
console.log(await tool.handler({ ... }, ctx));
```

**注意上游的 rate limit**,呼叫之間加 sleep。**跑完一定要刪掉腳本** ——
留在工作區會被下一輪 implementer 的 `git add -A` 掃進 commit。

### 把實測證據餵回去,不要轉述

發現問題時,`reject --reason` 裡貼**真實 API 的錯誤輸出全文**,比用自己的話
描述有效得多 —— 實測一輪就修對方向。同時告訴它哪些事**不用做**(例如
「這條驗收條件我已經驗過了」「這個案例是時段問題不是 bug,不用反覆打 API」),
省下它的 turn 與上游額度。

## 8. crash / 重開機之後

狀態每一步都存檔,所以 crash 是可以續跑的 —— 但要先分清楚**存到哪一步**:

```bash
python -c "
import json; d = json.load(open('.pipeline-state.json', encoding='utf-8'))
print('current:', d['current'])
for k, v in d['milestones'].items():
    print(k, {x: v[x] for x in ('phase','pr_number','session_id','review_round')})
"
```

**關鍵:`session_id` 只在 `imp.ask()` 回來之後才存。** 如果 crash 發生在
implementer 第一次回應之前,state 裡連這個 milestone 的紀錄都沒有 ——
寫出那些程式碼的 session 已經沒有 context 可以接續了。

這時候不要讓新 agent 接手來歷不明的半成品(它會假設那些程式碼是對的,
而沒有人驗證過)。正確做法是保存後重跑:

```bash
cd /path/to/target
git add -A && git commit -m "wip: 中斷的半成品(僅供事後對照)"
git branch -m wip/crashed-YYYYMMDD     # 空出 milestone/N-... 這個名字
git checkout master                     # 回到乾淨狀態
# 然後直接重跑 run;該 milestone 會從 implement 重新開始
```

**一定要空出 branch 名**,否則新 agent 的 `git checkout -b` 會失敗。
也記得刪掉 agent 留下的臨時檔(`tmp-*.py`、`smoke-*.mjs` 之類)。

## 9. 已知的坑

- **`tools` 才是工具白名單,`allowed_tools` 不是。** 後者只是「免詢問」。
- **`implementer_cost_usd` 是覆寫語意**(一個 milestone 一個持久 session 的前提),
  **resume 會打破這個前提** —— 新 session 從 0 開始累計,把先前的花費蓋掉,
  帳面會嚴重低估。`reviewer_cost_usd` 是累加的,那個數字是準的。
- **agent 的錯誤要自己檢查,SDK 不會丟例外。** API 失敗時 `is_error=True` 但
  `subtype` 仍是 `"success"`,只看其中一個會漏。
- **`VERDICT` 解析不到時 fail-closed(視為要求修改),`UNRESOLVED` 解析不到時
  fail-open。** 方向刻意相反,不要「統一」。
- **reviewer 無法 approve 自己帳號開的 PR。** prompt 已指示改用 `gh pr comment`,
  流程不依賴 PR 的 review 狀態,verdict 走 stdout 解析。
- **每次 park 都會 exit 1**,所以 `notify.channels` 不設就等於沒人知道它停了。

## 10. 一個 milestone 的典型節奏

```
implement (20–40 min) → PR 開啟
  → review 第 1 輪 (5 min) → REQUEST_CHANGES(通常 3–5 個阻擋項)
  → fix (10–20 min)
  → review 第 2 輪 (5 min) → APPROVE
  → park 在 merge gate
  → 【人】跑真實驗收 → reject 補洞 或 approve
  → merge → 下一個 milestone
```

順利的話一個 milestone 約 1–1.5 小時、$15–25。要人介入 2–3 次
(agent_error 續跑 + merge gate 決策)。

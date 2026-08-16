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
  verify_command: "pytest -q"    # 見下方「確定性驗收關卡」
  verify_timeout_sec: 900

notify:
  channels: [desktop, pr_comment]   # park 之後會 exit,沒通知不會知道
  mention: "@your-github-handle"

state_file: .pipeline-state.json
```

### `verify_command`:merge 前的確定性關卡

reviewer 的 `VERDICT` 是 LLM 的判斷;這一道是 code 驗出來的事實。填目標 repo
的測試/lint 命令(走 shell,所以 `pytest -q && ruff check .` 可以),**強烈建議填**
—— 沒填的話「有沒有通過測試」完全靠 implementer 自律,而 orchestrator 從不驗證。

- 在 **PR 分支**上跑,APPROVE 之後、merge 之前。
- 失敗 → 輸出當成這一輪的意見交回 implementer,**共用 `max_review_rounds`**,
  不會無限重試(輪數用完就落到 stuck)。
- implementer 那輪什麼都沒改時不重跑,只會在 log 留一行
  「workspace 沒有變動」warning —— 看到它代表迴圈空轉了,值得人看一眼。
- `verify_timeout_sec` 要比測試實際跑的時間寬鬆(逾時算失敗)。
- 目標 repo 沒有測試就先留空,不要為了填而填一個永遠會過的命令。

### `reviewer.type`:三選一

| 值 | 怎麼運作 | 什麼時候用 |
|---|---|---|
| `script` | 每輪開一個 fresh Claude session 讀 diff、跑測試、下 verdict | 預設,單機無人值守 |
| `actions` | 輪詢 GitHub Actions 上跑的 review | review 已經跑在 CI 上 |
| `hybrid` | 先用 `ocr delegate` **確定性地**算出「該審哪些檔、每個檔套哪組檢查項目」,再把這份清單交給 Claude 讀 diff / 跑測試 / 驗收驗收條件 / 下 verdict | 想讓 review 有一份不受 LLM 心情影響的覆蓋清單 |

啟用 `hybrid` 只要兩步:

```bash
npm i -g @alibaba-group/open-code-review     # 提供 `ocr` 這支 CLI
```

```yaml
reviewer:
  type: hybrid
  model: opus
  # 下面全部可省略,列出來只是說明有哪些旋鈕
  ocr_exe: ocr                 # 執行檔名稱或完整路徑
  ocr_timeout_sec: 300
  ocr_exclude: ""              # 逗號分隔的 gitignore 樣式
  ocr_rule_path: ""            # 自訂規則 JSON
  ocr_max_rule_chars: 40000    # 超過就截斷(prompt 會註明截斷了)
```

**不需要 OCR 自己的 LLM 金鑰。** 走的是 `delegate` 模式 —— 它標榜 `no LLM required`,
只做檔案篩選與規則比對,判斷全部由 reviewer 的 Claude session 做。所以訂閱制的
Claude 認證就跑得動,也不會有第二筆帳單。**不要改成 `ocr review`**,那個模式要它
自己的 API key,訂閱制認證餵不進去。

幾件要先知道的:

- **它是輔助,不是關卡。** delegate 模式下 OCR 連程式碼都沒讀過,verdict 仍然只由
  Claude 下。它給的規則開頭寫著 `Favor precision over recall` —— 那是它的取捨,
  不是你的。`hybrid_review_prompt` 已經明講「檔案清單是**下限**、規則是**提醒**」。
- **它不跑測試、不看 milestone 驗收條件**,而且會用 `unsupported_ext` 略過 `.md`
  之類的檔案(prompt 會把那些點名回來)。所以 `hybrid` 不能取代 §7 的人工 merge gate ——
  **這條現在有實測佐證了,見下面的「盲點」。**
- **這一段是 fail-open**:沒裝、逾時、JSON 壞掉,都只記警告 + 把失敗原因寫進 prompt,
  不 park。方向和 `VERDICT` 的 fail-closed 相反,理由同 `UNRESOLVED` ——
  下游還有 `VERDICT` 擋著,若這裡也 fail-closed,一台沒裝 `ocr` 的機器會每輪都停。
  **代價是「OCR 長期悄悄沒在跑」不會自己浮出來**,起飛時看一次 log 有沒有那行警告。

#### 實測(formosa milestone 5,2026-08-15/16,第一筆 hybrid 數據)

M1–M4 跑 `script`,M5 換 `hybrid`,同一個 repo、同樣 opus、同一個人在 merge gate 驗。
M5 是平台整合類(離線建索引 + KV 分片查詢),20 個檔案。

| 項目 | hybrid(M5) | script(M1–M4) |
|---|---|---|
| 每輪 reviewer 成本 | $18.14 / 8 輪 ≈ **$2.3** | $2–3 |
| `reviewer.max_turns` | **40 不夠,要 100** | 40 四個 milestone 都沒撞過 |
| OCR delegate 的產出 | 每輪「11–12 個檔案待審、7–9 個被略過、3 組規則」,耗時 2 秒 | — |

**結論:值得用,但一定要跟著把 `max_turns` 調高。** 每輪成本相近,貴的是輪數;
但 hybrid 多拿一份待審清單、會逐檔去讀,40 輪在第 2 輪就撞頂 park 了
(而且是 SDK 丟例外那條路,不是 agent 自陳,見 §9)。

**review 深度確實比較好。** M5 的 reviewer 每一輪都**重跑驗證前幾輪的修正**
(「我不是看回覆信,是重跑過」),第 4 輪還自己壓了 6 個 OCR 清單沒提的方向
(`__proto__` 當 gram、manifest `built_at` 壞掉會不會噴 NaN、KV bulk 的 JSON
轉義膨脹實測 ×1.051)。但這**無法乾淨地歸因給 OCR** —— delegate 模式下它連
程式碼都沒讀過,產出只有檔案清單與規則,深度更可能來自 opus 本身。

##### 盲點:`.md` 被略過,而過期的文件宣稱就藏在那裡

每一輪都有 **7–9 個檔案被 `unsupported_ext` 略過**,主要是 `.md`。
`hybrid_review_prompt` 有把它們點名回來,但**四輪 review 都沒抓到**下面這件事:

> M5 讓遠端版的搜尋恢復可用,但 `docs/w5-notes.md` 仍寫著
> 「`search_datasets` 在遠端版**預設停用**」,而且沒有任何指向新筆記的指路。
> 那份檔案不在這個 PR 的 diff 裡,所以連「被略過」都算不上 —— 它根本沒進清單。

這是人在 merge gate 用 `grep` 找出來的(§7 的「去對 agent 自己寫過的數字」),
一分鐘的事。**這正是 §7 說的模式:agent 在 milestone N 量過 / 寫過的東西,
到 N+1 就退化成過期的宣稱,而 reviewer 不會跨 milestone 去翻舊筆記。**

所以 hybrid 改變的是「這一份 diff 審得多細」,**沒有**改變「diff 之外的東西沒人看」。
merge gate 的人工驗收該做的事一件都沒少。

### 實測基準值(opus,中型 milestone)

| 項目 | 實測 |
|---|---|
| 一個 milestone 的實作階段 | 20–40 分鐘,$10–15 |
| 六~七個 tool + 測試 + fixtures 的規模 | 40+ 檔案、4000–7000 行 |
| `max_turns: 120` | **不夠** —— opus 把程式全寫完了,卡在 commit / push / 開 PR 之前 |
| 一輪 review(40+ 檔案的 diff) | $2–3 |
| `reviewer.max_budget_usd: 3.0` | 太薄,第一輪就用掉 $2.05 |
| `reviewer.max_turns`(`script`) | 40 夠(M1–M4 沒撞過) |
| `reviewer.max_turns`(`hybrid`) | **40 不夠,要 100** —— 多一份待審清單要逐檔讀,第 2 輪就撞頂 |
| **難的 milestone 會超出這個級距很多** | 一個平台整合(Cloudflare Workers)跑了 **7 輪 review、約 $58**:implementer $36、reviewer $15(累加 7 輪)、外加一次人工打回 |

最後一列是重點:**成本主要由 review 輪數決定,不是由實作規模決定。**

reviewer 每輪都是 fresh session,所以它天生不會收斂 —— 每一輪都是一個新的
資深 reviewer 用新鮮眼睛掃同一份 diff,挑得出**另一組**意見。收斂靠的是
orchestrator 依輪數插進 prompt 的兩段約束(`prompts.round_notes`):

- reviewer 完整掃過一次之後:新發現只有**五類**才能擋合併(錯誤行為、資安、
  milestone 規格沒做到、測試沒過、**對外承諾與實際行為不符**),其餘寫成
  「後續建議」。
- 最後一輪:只擋 blocker。**但這段只在 `merge_gate: ask` 且該 milestone 已進入
  gate 範圍時才會插入** —— 沒有人在後面把關的話,它等於「輪數用完自動 merge」。
  用 `merge_gate: auto` 的人不會拿到這個放寬,輪數用完照樣落到 `stuck`。

**該看的指標不是總輪數,是「第 3 輪之後的 REQUEST_CHANGES 裡 blocker 佔幾個」。**
比例接近 1 表示已經沒有收斂空間可省,輪數多是問題本身難;比例低才表示 reviewer
在開新戰場,那時候該人工介入(`reject` 把爭點講死)而不是讓它繼續磨。
另一個免費的訊號:每輪修正的 diff 有沒有變窄(3 個問題 → 1 個 → 1 個是在收斂)。

`max_turns` 用盡與預算用盡都是 `park`(有 `gate_on_agent_error: true`),
不是崩潰 —— 但每次都要人介入很吵,寧可一開始就給夠。

## 4. 起飛前檢查

```bash
# 缺任何一項都會在半路才炸
claude --version          # SDK 要靠它起 agent
gh --version && gh auth status
node --version            # 目標專案需要的話
python -c "import claude_agent_sdk; print('sdk ok')"

ocr --version            # 只有 reviewer.type: hybrid 需要
```

**`gh` 一定要在 PATH 上。** orchestrator 的 `gh.py` 用
`subprocess.run(["gh", ...])`,implementer 要 `gh pr create`,reviewer 要
`gh pr diff` —— 全靠 PATH 解析。失敗症狀很難認:agent 實作完、push 完,
然後 orchestrator 丟「找不到 open PR」,一個 milestone 的預算就白燒了。
Windows 上 gh 可能裝在 `%LOCALAPPDATA%\Programs\gh\bin`(不是 `GitHub CLI\`)。

其他:remote 是 SSH 的話要有 ssh-agent(orchestrator 自己會 `git fetch`/`pull`);
目標 repo 的 base branch 要乾淨;`.env` 金鑰填好。

**填了 `verify_command` 的話,先自己在目標 repo 手動跑一次。** 它在 base branch
上就該是綠的 —— 不然第一個 milestone 一 APPROVE 就會被自己的驗收命令打回,
白燒一輪 implementer。

### 跑在持久化終端裡

前景跑的話終端一關那一輪就斷。`.pipeline-state.json` 讓下次可以續跑,但
implementer 那 20–40 分鐘 / $10–15 已經付掉了。**開跑前先決定怎麼掛著。**

Windows(本機環境,不用裝新工具,建議):

```powershell
Start-Process python -ArgumentList '-m','milestone_pipeline','run','--config','target.yaml' `
  -RedirectStandardError pipeline.log -WindowStyle Hidden
Get-Content pipeline.log -Wait -Tail 50      # 隨時 attach 看即時輸出,ctrl+c 離開不影響 pipeline
```

(agent 的文字會即時寫進這個 log,見 §5。)開機也要自動跑就改用「工作排程器」。

macOS / Linux(例如整條 pipeline 跑在另一台機器上、從本機 `ssh` 進去看):

```bash
ssh host 'cd ~/Documents/agents-dev-tools && \
  nohup .venv/bin/python -m milestone_pipeline run --config target.yaml \
    >> ~/pipeline.log 2>&1 < /dev/null & echo $! > ~/pipeline.pid'

ssh host tail -n 50 ~/pipeline.log       # 隨時回來看,離開不影響 pipeline
ssh host 'kill $(cat ~/pipeline.pid)'    # 中途叫停(§8)
```

三個細節:**`< /dev/null` 不能省**(少了它 ssh 會等 stdin 而不返回);
log **不要放 `/tmp`**(macOS 會清);PID 檔讓「merge 完就停」那招(§8)
從任何一台機器都下得了手。

**orchestrator 不需要 tmux。** 它從來不讀 stdin(park 是存檔 → 通知 → exit,
刻意不 `input()` 等人,見 §6),所以「接上一個互動終端」在這裡是零收益 ——
attach 就是讀那個 log,而 agent 的文字會即時落地(§5),離線期間發生的事一件不漏。

**但守護 agent 需要**(§7),它剛好相反:是互動 session,而 unsnooze 的喚醒方式
就是往它的 pane 裡打字。`guard` 會自己處理,不用你手動開 —— 別把上面那段
`nohup … &` 套到 `guard` 上,你會得到一個沒有 TTY、ssh 一斷就死、
unsnooze 也叫不醒的 session。

**同一份存檔只能有一個 `run`。** `run` 進場會拿一個 OS 檔案鎖
(`<state 檔>.lock`),已經有人拿著就直接停下來並告訴你。兩個 orchestrator
併跑不會噴錯,只會互相覆蓋 `.pipeline-state.json` —— 症狀出現時已經是
「implementer 接不回 context」或「同一個 milestone 開了兩個 PR」。
鎖檔在 crash / `kill -9` 之後由 OS 自己放掉,沒有殘骸要清。

**`session_id` 綁在跑它那台機器的 `~/.claude/projects/` 底下,不能跨機器 resume。**
所以要換機器就在 milestone 之間換(前一個 merge 完、下一個還沒起跑),
不要 milestone 跑到一半才搬 —— implementer 的 context 接不回去。
`repo.path` 寫成 `~/Documents/...` 的話同一份 config 兩台都指得到,不必養兩份。

**注意 `notify.channels` 的 `desktop` 是寫死 PowerShell 的
(`notify.py`),在 mac / Linux 上會失敗** —— 通知失敗不會中斷 pipeline(只記 log),
但等於沒有桌面通知,要留一條 `pr_comment` 或 `webhook`。

另一條路是 [herdr](https://github.com/herdrdev/herdr)(agent-aware 終端多工器 +
背景 server):`herdr` 起 server → 在 pane 裡跑 → `ctrl+b q` 離開 → 之後
從任何終端 reattach。生態系有現成的遠端監看(collie PWA + push、herdr-remote、
ccgram Telegram)。**但它的 Windows 支援仍標示 beta**,本機是 Windows 10,
沒有非它不可的理由就用上面那招。

## 5. 跑

```bash
# 長時間跑,丟背景並導向 log
python -m milestone_pipeline run --config target.yaml >> /tmp/pipeline.log 2>&1

python -m milestone_pipeline status --config target.yaml   # 看進度
```

exit code:`0` = 全部完成;`1` = 停下來等人或流程錯誤。

**agent 的文字會即時進 log**,每則帶 `[implementer]` / `[reviewer]` 前綴,
所以不用等 20–40 分鐘才知道它在幹嘛:

```powershell
Get-Content pipeline.log -Wait -Tail 50                      # 全部
Select-String '^\S+ INFO \[implementer\]' pipeline.log       # 只看 implementer
```

只有 `/compact` 那段刻意不輸出(是雜訊)。

**orchestrator 自己的 log 仍然很稀疏**(只在階段轉換時寫)。agent 的輸出也停了
的話,再去看目標 repo 的 git 痕跡:

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

**`approve` 會繞過 `verify_command`。** 它直接把 phase 設成 merge —— 人已經
裁決過了。所以「我想再跑一次驗收」不能用 `approve`,要用 `reject`。

### `reject` 才是「接續中斷的工作」的正確工具

名字有誤導。當 implementer 修到一半被預算/輪數切斷時:

- `approve` → 回到 review 階段、輪數 +1 → **再叫一次 reviewer**(多花一輪錢),
  而它看到的 diff 還沒有那些未 commit 的修改,大概率把同樣的意見再講一遍。
- `reject --reason "接續你被中斷的修復,意見全文在 PR #N 的 comment 裡"` →
  跳過 reviewer,直接讓 resume 的 session 接著做。**這才是對的。**

`reject` 與 `retry` 都會清掉「reviewer 已完整掃過」的旗標,所以**下一輪 reviewer
會拿到一次不受限的完整掃描**(而不是「只確認先前意見有沒有處理」)。這是刻意的
—— 人接手之後 implementer 往往改了遠超出你意見範圍的東西。代價是那一輪 review
比較貴,別把它誤判成迴圈退步。

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
- 一份部署文件寫著「其他 tool 完全正常,拿的都是幾十 KB」,而**同一個 repo 自己的
  實測筆記**寫著那兩個 tool 是 1.15 MB 與 442 KB。打回去要求量,量完發現三個 tool
  真的超過平台上限。

最後那一條是最值得學的模式:**去對 agent 自己寫過的數字**。agent 在 milestone N
量過的東西,到 milestone N+2 常常變成沒有根據的概括。這種矛盾 fixtures 測不出來,
reviewer 也不一定會跨 milestone 去翻舊筆記,但它會變成別人照著做卻踩空的文件。
grep 一下宣稱(「完全正常」「綽綽有餘」「不影響」)再去翻 `docs/*-notes.md` 的
實測表,成本很低。

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

### 交給守護 agent 顧(無人值守)

上面那份清單就是守護 agent 的工作內容 —— 它是 park 點上的那個人。

```bash
python -m milestone_pipeline guard --config formosa.yaml
```

有 tmux 的話它會**起在背景的 tmux session 裡**,名字從 config 檔名推出來
(`formosa.yaml` → `guard-formosa`)。那個名字同時是三件事:

```bash
tmux has-session -t guard-formosa   # 有沒有守護 agent 在顧這條 pipeline
tmux attach -t guard-formosa        # 回去看它(ctrl+b d 離開,不會中斷它)
tmux kill-session -t guard-formosa  # 換掉它
```

**所以「先檢查有沒有」不需要另外發明 pid 檔** —— session 不在了就是不在了,
沒有殘骸要判斷。再跑一次 `guard` 也不會開出第二個:偵測到同名 session 就
印出 attach 指令然後退出。(兩個守護 agent 會對同一個 gate 各自下決策 ——
一個 approve 一個 reject,而且兩個都會去重啟 `run`。)

從另一台機器起飛就是普通的 ssh,不用 `nohup`,tmux 已經處理了持久化:

```bash
ssh mac 'cd ~/Documents/agents-dev-tools && \
  .venv/bin/python -m milestone_pipeline guard --config formosa.yaml'
```

起飛前它會警告(**只警告,不擋**)工作目錄有未 commit 的變動、或落後 upstream
—— 要防的是「跑到的 orchestrator 不是 git 裡那份」。**刻意不看在不在 `master` 上**:
分支名不是「code 乾不乾淨」的代理(`guard` 這個功能自己就是在 feature branch
上寫的),拿它當代理是 CLAUDE.md 裡 `_SCOPE_LOCK` 那顆雷的同一個形狀。

**它被關掉了所有寫入工具**(`Edit` / `Write` / `NotebookEdit` /
`git commit` / `git push` / `gh pr merge`,見 `guard._DENY`)。這不是保守,
是實測過的:formosa M8 那輪,守護 agent 在 merge gate 上發現 `README.en.md`
自相矛盾(免金鑰 tool 數一處寫 11、一處寫 9),判斷「一行的事,直接修比
reject 跑一整輪省 10 分鐘和幾塊美金」,於是 commit 進分支。時間軸:

```
21:47:16  reviewer APPROVE
21:47:16  orchestrator 跑 verify → 綠
21:48:51  ← 守護 agent commit 45d74e5
21:49:00  GitHub 才開始跑這個 sha 的 CI
21:49:20  merge                        ← CI 還在跑
21:49:54  CI 才結束
```

那個 commit 是唯一一個 reviewer 沒看過、verify 沒跑過、merge 時 CI 還沒結束的。
結果沒事(內容真的改對了),但那條路徑上一道關卡都沒有。
**它讀得懂規矩,規矩當時只是文字** —— 同 CLAUDE.md 第一條,確定性的事要留在 code 裡。

擋不住的殘留:`gh issue close` / `gh pr comment` 這類不改內容的寫入還是通的
(關掉一個已經修掉的 issue 是它該做的事),但 `--disallowed-tools` 是**權限
deny 規則**,不是把工具拿掉 —— 語意上跟 SDK 那邊 `tools=` vs `allowed_tools`
的差別一樣,只是互動模式下沒有 `--tools` 可用(那個旗標只吃 `--print`)。

#### unsnooze:撞到用量上限之後自己接回來

有裝 `unsnooze` 的話 `guard` 會自動用它包住這個 session。撞到 5 小時 / 週上限時,
額度重置後它會喚醒守護 agent,守護 agent 再去重啟 `run` —— 兩層都恢復,
無人值守的跑才接得下去(formosa M9 就是被
`You've hit your session limit · resets 7:10am` 打斷、park 在 `agent_error` 的)。

**不要跑 `unsnooze install`。** 那會裝全域 hook,對機器上每個 claude session 生效;
`unsnooze [claude args...]` 這種 launcher 用法天生只包住守護 agent 自己。
裝過的話 `guard` 啟動時會警告,用 `unsnooze uninstall` 拆掉。

**Windows 上不會有 auto-resume**,unsnooze 靠 tmux / Zellij 的 pane 恢復,
Windows 兩個都沒有 —— 沒 tmux 就退回前景跑(關掉終端就沒了),`guard` 會把
這兩件事都印出來然後照常跑(deny 與 prompt 兩邊都是跨平台的)。
這不影響實務:implementer 的 session_id 綁在跑它那台,pipeline 本來就固定
在同一台跑完(見 config 開頭)。

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

### 中途叫停(例如「merge 完這個 milestone 就停」)

`run` 會一路跑到全部做完,沒有「只跑一個 milestone」的開關。要停在某個
milestone 之後,就盯 log 等 `PR #N 已 merge` 出現再砍掉 process ——
狀態在那行 log **之前**就存好了,所以砍掉是安全的。

砍晚了的話下一個 milestone 已經起跑,要收尾:

```bash
# state 裡沒有它的 session_id → 半成品沒有人接得下去,清掉重來
git checkout master                    # 未 commit 的改動會跟著跳過來
git restore <那些被改的檔案>            # 丟掉半成品
rm <新增的未追蹤檔案>
git branch -D milestone/N-...          # 先確認 rev-list --count master..HEAD 是 0
```

**盯 log 時不要用 `[IO.File]::ReadAllText`**(PowerShell):`Start-Process` 的
redirect 還握著檔案,會一路 `IOException`。用 `Get-Content -Tail` 或帶
`FileShare.ReadWrite` 開檔。

## 9. 已知的坑

- **`tools` 才是工具白名單,`allowed_tools` 不是。** 後者只是「免詢問」。
- **`implementer_cost_usd` 是覆寫語意**(一個 milestone 一個持久 session 的前提)。
  resume 會開新 session、SDK 從 0 重新累計,所以 `MilestoneState` 用
  `implementer_cost_base_usd` 把兩段接起來(`rebase_implementer_cost()` /
  `record_implementer_cost()`)。**不要直接指派 `implementer_cost_usd`**,
  那會退回舊的低估行為。`reviewer_cost_usd` 是單純累加的。
- **agent 的錯誤有兩條路,兩條都要接。** `is_error=True` 是 agent 跑完但結果不好
  (輪數/預算用完、上游 429),SDK **不丟例外**,而且此時 `subtype` 仍可能是
  `"success"`,只看其中一個會漏。另一條是 SDK / CLI 控制平面自己出錯
  (實測過 `Exception: Claude Code returned an error result: success`)、
  連線中斷、CLI process 被外力砍掉 —— 那些是**真的丟例外**。orchestrator 已經把
  兩條都收成 `agent_error` park;新增任何 agent 呼叫時兩條都要照顧。
- **存檔早於動作,所以中斷會留下「已計數但沒發生」的痕跡。** `review_round` 是在
  呼叫 reviewer **之前**就 +1 存檔的(這樣才不會漏記已發生的事),代價是崩在
  呼叫途中會白吃一輪。orchestrator 的例外處理會把輪數退回去;若是更早版本留下的
  狀態,用 `retry` 手動歸零。
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

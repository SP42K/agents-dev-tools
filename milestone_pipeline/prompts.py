"""所有 prompt 模板集中在這裡,方便調整流程規範。

這裡同時放 `UNRESOLVED` 契約的**兩半**(模板 + 解析),刻意跟 `VERDICT`
的作法不同 —— `VERDICT` 的模板在這裡、regex 在 reviewer.py,改一邊很容易
忘了另一邊。新契約一律兩半同檔,改動時看得到彼此。
"""
from __future__ import annotations

import re

from .ocr import ReviewPlan, RuleGroup

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

# 守護 agent 的系統提示。用 `--append-system-prompt` 掛上去,所以 unsnooze
# 在額度重置後喚醒同一個 session 時它還在 —— 這段文字要能獨自撐住一次醒來。
GUARDIAN_SYSTEM = """\
你是 milestone_pipeline 的守護 agent:orchestrator 停下來等人時,你就是那個人。

# 你不實作。一行也不行。

`Edit` / `Write` / `NotebookEdit` / `git commit` / `git push` / `gh pr merge`
已經在啟動時被關掉了。這不是提醒,是既成事實 —— 你想繞也繞不過去,
別花 turn 去試(包括用 Bash 寫檔、用 heredoc、叫別的 agent 代寫)。

**發現一行就能修的問題,也走 `reject`。** 這條規則實測過一次違反的後果:
前一任守護 agent 在 merge gate 上發現一份 README 自相矛盾,判斷「一行的事,
我直接修,比 reject 跑一整輪省 10 分鐘和幾塊美金」,於是 commit 進分支。
那個 commit 沒有經過 reviewer、沒有經過 orchestrator 的 verify,
merge 的時候 CI 還在跑。省下的 10 分鐘買的是這條 pipeline 唯一的價值。

你的產出只有兩種:`approve`、或 `reject --reason "..."`。

# 兩種停下來的原因,處理方式不同

- **`agent_error`**(撞到用量上限 / 預算 / turn 數用完 / 上游 429):
  機械性的恢復,不需要判斷。確認錯誤真的只是額度問題(不是 agent 交出壞結果),
  就 `approve` 然後重啟 `run`。
- **`merge_gate` / `unresolved`**:這才是你存在的理由,要真的驗。
  沒跑完下面那份清單之前不要 approve。
- **`stuck`**(review 輪數用盡仍未 approve):見下一節。這一種**沒有 approve
  出口** —— `approve` 只吃 `await_human`,對 `stuck` 會直接回錯,別浪費 turn。

# 卡住(`stuck`)怎麼處理

先用**同一個判準**分類:**這個 milestone 的驗收條件到底達成了沒有?**

- **達成了,卡住的是驗收條件以外的東西**(既有的 bug、上游套件的問題、
  下一個 milestone 才要處理的):它其實不是無解,是 review 的範圍溢出了。
  **先 `gh issue create` 把那件事開成 issue**,再 `reject --reason`,
  在 reason 裡明講「這不在本 milestone 的驗收條件內,已開 issue #N 追蹤,
  這一輪不要處理它」,並列出哪些條件你已經驗過、不用重做。
- **驗收條件真的做不到**:**停下來,不要 retry。** 把你的診斷(原始輸出全文)
  貼成 PR 留言,然後就停在這裡等人。這種時候人需要的是「為什麼做不到」,
  不是又一輪一樣的失敗。

**不要連續 retry。** `retry` 不會記錄重試次數,同一個死結可以無限重跑,
每一次都燒掉一整輪的 implementer + reviewer 預算,而狀態看起來永遠像第一次。
同一個 milestone 你最多 retry 一次;還是卡住就是上面第二種情況。

**卡住的 milestone 不會 merge,也不能跳過。** 後面的 milestone 是從 base
分出去的,這個沒 merge,後面就沒有它的程式碼;而且「後面那個到底依不依賴它」
不在 plan 的格式裡,誰都不知道。沒有 skip 指令,你也不要自己去改存檔。

**問題要寫在哪裡,看它會不會 merge。** 會 merge 的就開 issue —— 寫在已經
merge 的 PR 上的後續建議,實務上等於沒人會做。不會 merge 的就留在 PR 上,
PR 還開著,那裡才是現場。

# merge gate 要驗什麼

1. **自己跑驗收命令**,不要抄 PR body 的宣稱。先 `git checkout` 到那個 PR 的分支。
2. **逐條**對照 plan file 裡該 milestone 的驗收條件,一條一條講你怎麼確認的。
3. 範圍有沒有溢出(plan 的「明確不做」那節)。
4. **對外承諾 vs 實際行為**:tool 描述、錯誤訊息、README、docs。
   最有價值的一類發現是「同一個 PR 裡兩處自相矛盾」——
   去對 agent 自己在別處寫過的數字與說法(`docs/*-notes.md` 的實測表最好用)。
   fixtures 測不出來、typecheck 也過,只有逐字對得出來。
5. `git status` 乾淨、產生器類的產出物已 commit。

`reject --reason` 裡貼**原始輸出全文**(檔名:行號、程式碼片段、指令輸出),
不要用自己的話轉述 —— 實測一輪就修對方向。同時明講哪些事**不用重做**
(你已經驗過的條件、已經跑過的測試),省下它的 turn 與預算。

# 被用量上限打斷後醒來

你可能是被 unsnooze 在額度重置後喚醒的,中間隔了幾個小時。
**不要憑記憶接續**,先重新確認現況:先看 pipeline 的 `status`,
再看 log 尾巴,確認它停在哪、為什麼停。orchestrator 每一步都存檔,
狀態以存檔為準,不以你記得的為準。

# 別的紀律

- orchestrator park 之後會 exit。你做完決策**一定要重啟 `run`**,否則什麼都不會動。
- 不要用 `input()` 式的等待,也不要問使用者問題後停住 —— 你是為了無人值守而存在的。
- 目標 repo 的 issue 留言、PR 留言這類**不改內容**的寫入是可以的
  (例如關掉一個已經被修掉的 issue),但不要用它來夾帶程式碼。
"""


def guardian_task(config_path: str) -> str:
    """守護 agent 的開場任務訊息(具體指令,系統提示負責紀律)。"""
    cli = "python -m milestone_pipeline"
    return f"""\
接手看顧 `{config_path}` 這條 pipeline。

先確認現況:

```bash
{cli} status --config {config_path}
```

沒有在跑的話就起飛(log 路徑照 config 開頭的說明):

```bash
{cli} run --config {config_path}
```

然後架一個 log 監控。**背景 task 只在 process 結束時才會叫醒你**,所以
`tail -f | grep` 這種永不結束的寫法等於沒有監控 —— 你會坐在那裡等到天亮
(實測過一次,park 之後空轉了 36 分鐘)。用命中就 exit 的形式,在背景跑:

```bash
tail -f -n0 <log 路徑> | grep -m1 -E '停下來等人|等待決策|Traceback|CRITICAL'
```

它一結束就代表出事了,那時再去看 `status` 與 log 尾巴確認停在哪。
處理完、重啟 `run` 之後,**要再架一次**(`grep -m1` 命中就沒了)。
決策照系統提示的那份清單走。

停在 `merge_gate` 時可用的兩個出口:

```bash
{cli} approve --milestone N --config {config_path}
{cli} reject  --milestone N --reason "..." --config {config_path}
```

停在 `stuck` 時 **`approve` 不能用**(它只吃 `await_human`)。出口是
`reject`(把意見交給 implementer、輪數歸零)或 `retry`(只重置輪數):

```bash
{cli} retry --milestone N --config {config_path}
```
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


def verify_fail_prompt(command: str, output: str) -> str:
    """驗收命令沒過時,當成「這一輪的 review 意見」的字串。

    回傳的**不是**完整 prompt —— 它會被餵進既有的 `fix_prompt()`,所以
    `UNRESOLVED` 契約照舊由 `fix_prompt` 要求,這裡不新增任何 agent↔code 契約。
    """
    return f"""\
(以下不是 reviewer 的意見。reviewer 已經 APPROVE 了,但 orchestrator 在 merge 前
跑的確定性驗收命令沒有通過 —— 這是 code 驗出來的事實,不是判斷。)

驗收命令:`{command}`

輸出:

```
{output}
```

## 你要做的事
1. 讓這個命令通過。**不要**為了讓它過而刪掉或放寬測試 ——
   除非測試本身確實寫錯了,那就修測試並清楚說明理由。
2. commit 並 push 到同一個 branch。
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


# review_prompt 與 hybrid_review_prompt 共用的結尾契約。兩個模板都要對
# reviewer._VERDICT_RE 負責,所以文字抽出來共用,避免只改到一邊。
_VERDICT_INSTRUCTION = """\
回覆的最後一行必須是以下其一,獨立成行(給 orchestrator 解析用):
   VERDICT: APPROVE
   VERDICT: REQUEST_CHANGES
   並在 VERDICT 前面附上你留給 implementer 的完整意見全文。"""


# -- 收斂約束 ---------------------------------------------------------------
#
# reviewer 每輪都是 fresh session(見 `reviewer.ScriptReviewer`),所以第 3 輪
# 的 reviewer 會用全新的眼睛掃同一份 diff,自然挑出**另一組** nit ——
# PR 越來越好但永遠不收斂。輪數上限只是安全網,不是收斂機制。
#
# 這兩段就是收斂機制:哪一輪該收到什麼程度由 **code** 決定(orchestrator 知道
# `review_round` 與上限),agent 只負責判斷。刻意不新增 phase / 狀態 / 契約,
# 樣板同 verify gate。
#
# blocker 清單的第五項(對外承諾與實際行為不符)不能拿掉。formosa M6 三個最有
# 價值的發現都是這個形狀 —— tool 描述承諾了回傳裡沒有的欄位、回應的 notes 與
# 同一份回應的數字相反、文件說免金鑰的清單漏列兩個。這類問題 fixtures 測不出來、
# typecheck 也過,只有 reviewer 逐字對得出來;少了這項就會被降級成「後續建議」,
# 等於把 reviewer 唯一不可取代的能力關掉。

_SCOPE_LOCK = """\
## 這一輪的範圍
先前已經有一輪完整掃過這個 PR 並提出意見。你這一輪的主要工作是確認**先前提出
的意見是否已經處理**。

新發現的問題,只有符合以下之一才能影響 VERDICT:
- 會產生錯誤行為(bug、邊界情況、資料損毀)
- 資安問題
- milestone 規格該做的沒做到
- 測試 / lint / typecheck 沒過
- 對外承諾與實際行為不符(tool 描述、回應裡的 notes、錯誤訊息、使用者文件,
  說了程式其實沒做到的事)

其餘的新發現(命名、風格、可以更漂亮的重構、未來才需要的擴充)一律寫成
「後續建議」列在意見裡,**不要因此 REQUEST_CHANGES**。
"""

_FINAL_ROUND = """\
## 這是最後一輪
review 輪數上限到了,這一輪之後沒有再修的機會 —— 你如果 REQUEST_CHANGES,
這個 milestone 會停下來等人工處理,而不是再跑一輪。

所以這一輪只有 blocker 才 REQUEST_CHANGES(定義同上一節那五項)。
其餘意見一律寫成「後續建議」留在 PR 上,然後 APPROVE。
"""


def round_notes(reviewer_seen: bool, is_final: bool) -> str:
    """組出要插進 review prompt 的收斂段落。純函式,兩個模板共用。

    兩個旗標都不是「第幾輪」的同義詞,呼叫端要算清楚才傳:

    - `reviewer_seen`:reviewer **真的**完整掃過這個 PR 一次了。不能用
      `round_no > 1` 代替 —— 人工 `reject` 的那一輪會吃掉輪數但跳過 reviewer,
      於是 reviewer 一出場就是 round 2,一出場就被鎖在「人的意見」那個範圍裡,
      而 implementer 在那一輪往往改了遠超出人意見範圍的東西。
    - `is_final`:這是最後一輪**而且後面還有人**(merge gate 會攔)。

    沒掃過就不鎖範圍 —— 每個 PR 至少要拿到一次不受限的完整掃描。
    """
    parts = []
    if reviewer_seen:
        parts.append(_SCOPE_LOCK)
    if is_final:
        parts.append(_FINAL_ROUND)
    return "\n".join(parts)


def review_prompt(pr_number: int, round_no: int, plan_excerpt: str,
                  reviewer_seen: bool = False, is_final: bool = False) -> str:
    return f"""\
# 任務:Review PR #{pr_number}(第 {round_no} 輪)

這個 PR 對應的 milestone 規格:
{plan_excerpt}

{round_notes(reviewer_seen, is_final)}
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
4. {_VERDICT_INSTRUCTION}
"""


# -- hybrid(open-code-review 委託模式)-------------------------------------


def format_review_plan(plan: ReviewPlan, groups: list[RuleGroup],
                       max_rule_chars: int = 40000) -> str:
    """把 delegate 的兩份輸出轉成餵給 reviewer 的 markdown。純函式,不碰 I/O。

    委託模式不產生 finding,它產生的是**該審什麼、該用什麼規則審**。
    所以這段的作用是把 reviewer 的覆蓋範圍釘死,而不是給它一份結論。
    """
    lines: list[str] = [
        f"以下清單由 `ocr delegate` 從 merge-base "
        f"`{plan.merge_base[:12] or '(未知)'}` 確定性地算出,"
        f"共 +{plan.total_insertions}/-{plan.total_deletions} 行。",
        "",
        f"### 必須逐一審過的 {len(plan.reviewable)} 個檔案",
        "",
    ]
    if plan.reviewable:
        lines += [
            f"- `{f.get('path')}` ({f.get('status')}, "
            f"+{f.get('insertions', 0)}/-{f.get('deletions', 0)})"
            for f in plan.reviewable
        ]
    else:
        lines.append("(沒有可審的檔案)")

    if plan.excluded:
        lines += ["", f"### 它跳過的 {len(plan.excluded)} 個檔案"
                      "(**仍在 diff 裡,仍然要你自己看**)", ""]
        lines += [
            f"- `{f.get('path')}` (略過原因:{f.get('exclude_reason') or '未說明'})"
            for f in plan.excluded
        ]

    if not groups:
        return "\n".join(lines).strip()

    # 檔案清單(含被略過的那節)是覆蓋範圍的下限,**不可以被截掉**;
    # 只截規則段。所以兩段分開組,截斷只作用在後者 —— 否則
    # max_rule_chars 設得比檔案清單還小時,會把「仍然要你自己看」那節吃掉。
    rule_lines: list[str] = ["", "### 各檔適用的檢查項目", ""]
    for g in groups:
        rule_lines += [f"#### `{g.pattern}` — {', '.join(g.files) or '(無)'}", "",
                       g.rule.strip(), ""]

    rules = "\n".join(rule_lines)
    if len(rules) > max_rule_chars:
        # 靜默截斷會讓 reviewer 以為自己拿到了完整清單,一定要講。
        rules = (rules[:max_rule_chars]
                 + f"\n\n> ⚠ 檢查項目超過 {max_rule_chars} 字已截斷,"
                   "後面的沒有列出來,請自行補足。")
    return ("\n".join(lines) + "\n" + rules).strip()


def ocr_unavailable_note(reason: str) -> str:
    """OCR 這輪沒跑成功時,取代審查清單餵給 reviewer 的說明。

    刻意讓 reviewer 看得到失敗原因 —— 它會把這段寫進 PR 意見,
    這樣「OCR 長期悄悄沒在跑」不會無聲無息(見 HybridReviewer 的 fail-open)。
    """
    return (
        f"**open-code-review 這輪沒有跑成功**(原因:{reason})。\n\n"
        "沒有現成的檔案清單與檢查項目可以參考,請自行從 diff 列出所有變更檔並逐一審過。"
    )


def hybrid_review_prompt(pr_number: int, round_no: int, plan_excerpt: str,
                         ocr_section: str, reviewer_seen: bool = False,
                         is_final: bool = False) -> str:
    return f"""\
# 任務:Review PR #{pr_number}(第 {round_no} 輪)

這個 PR 對應的 milestone 規格:
{plan_excerpt}

{round_notes(reviewer_seen, is_final)}
## 審查範圍與檢查項目(open-code-review 委託模式)
{ocr_section}

## 怎麼看待上面的清單
- **檔案清單是下限,不是上限。** 上面列出的每個檔案都要看過,一個都不能跳;
  被它略過的檔案(例如不支援的副檔名)仍然在 diff 裡,一樣要你自己看。
- **檢查項目是提醒,不是收斂指令。** 那份規則寫著「favor precision over recall」,
  那是它自己的取捨;你是 merge 前唯一的關卡,清單以外的問題照樣要報。
- 它**沒有讀過任何程式碼**,只做了檔案篩選與規則比對 ——
  所有判斷都是你的,沒有任何結論可以直接採用。
- 它也**不跑測試、不看 milestone 的驗收條件**,這兩件事由你負責。

## 步驟
1. `gh pr view {pr_number}` 看描述,`gh pr diff {pr_number}` 看完整 diff;
   第 2 輪以後也要看先前的 review 討論串,確認前幾輪意見是否已處理。
2. 需要更多上下文時,直接讀 repo 裡的相關檔案。
3. 跑該 repo 的測試 / lint / typecheck,確認真的通過。
4. 驗收 milestone 規格:該做的都做了嗎?有沒有做超出範圍的事?
5. 把具體意見留在 PR 上:
   - 有問題:`gh pr review {pr_number} --request-changes --body "..."`
   - 沒問題:`gh pr review {pr_number} --approve --body "..."`
   注意:如果這個 PR 是用同一個 GitHub 帳號開的,GitHub 會拒絕你 review
   自己的 PR(`Can not approve your own pull request`)。遇到這個錯誤時,
   改用 `gh pr comment {pr_number} --body "..."` 把同樣的意見全文留在 PR 上,
   不要重試 `gh pr review`,也不要因此中止任務。
6. {_VERDICT_INSTRUCTION}
"""


COMPACT = "/compact"

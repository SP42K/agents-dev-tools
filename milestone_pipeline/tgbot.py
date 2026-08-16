"""Telegram 控制端點:讓 park 點的決策可以從手機下。

`notify` 已經會在 park 時推 Telegram,但那是**單向**的 —— 收到「M6 等待決策」
之後人還是得回到電腦前 ssh 進去按 `approve`,而 Telegram 之所以是無人值守跑
唯一真的會被看到的 channel,正是因為人不在電腦前。看得到、動不了,park 的
等待時間就等於「人下次坐回電腦前」。

**這是這個 repo 第一個對外開放的入口。** 先前所有 Telegram 相關的 code 都是
單向送出去,缺設定時 fail-open 是對的(頂多收不到通知);**這裡方向相反**:
任何人知道 bot 的 username 就能傳訊息給它,所以

  1. `chat_id` 白名單是**唯一**的驗證,而且 `TELEGRAM_CHAT_ID` 沒設就拒絕啟動
     —— 不是警告。缺了不是「這台收不到通知」,是「誰都能對你的 repo 下 approve」。
  2. 指令表是**白名單**,不是黑名單:`parse_command` 認不得的一律 `None`。
     按鈕的 `callback_data` 走同一條路(`parse_callback`),**按鈕不是比較可信
     的輸入** —— 它跟打字進來的訊息一樣要驗。
  3. **不走 shell、不拼路徑。** 一律 argv list,專案名只能對到
     `guard.collect()` 掃出來的那份清單(查表)。
  4. 只能下既有的 CLI verb,**沒有 `/reset`** —— 不可逆的清除不該掛在一個
     手機打字打錯就會觸發的介面上。

bot 自己**沒有寫入能力**,這點跟守護 agent 一樣;差別在守護 agent 靠
`--disallowed-tools` 擋,bot 靠「只有這幾個 subprocess 跑得起來」擋。

它同時是 park 的 **watchdog**:`notify.reasons` 把 `merge_gate` 這種每個
milestone 都會來一次、而且守護 agent 本來就會處理掉的原因排掉之後,唯一還會
告訴你「守護 agent 沒把它處理掉」的就是這裡的 `escalate_after_min`。
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import guard
from .config import TELEGRAM_CHAT_ID_ENV, TELEGRAM_TOKEN_ENV
from .gh import Gh

log = logging.getLogger("pipeline")

# sendMessage 上限 4096,留餘裕(同 notify.TelegramNotifier)
_TG_LIMIT = 4000
# callback_data 的硬上限(Telegram 規格),超過 API 會直接回 400
_CALLBACK_LIMIT = 64

# 進度存到哪。**一定要落地**:不存的話 bot 重啟會把上一批 update 再吃一次,
# 而重播的是 `/approve` —— 那是真的會動到 state 的東西。
# 檔名進 `.gitignore`:它落在 orchestrator 自己的 repo 裡,而
# `guard.repo_warnings()` 看的就是 `git status --porcelain`,不排掉的話每次
# 起守護 agent 都會警告「有未 commit 的變動」。
OFFSET_FILE = ".mp-tgbot-offset"

_SEP = "─" * 22

# `/` 選單(setMyCommands)。手機上就不用背指令了。
_COMMAND_MENU = [
    ("guards", "全部 pipeline 的狀態"),
    ("status", "<專案> 單一 pipeline 的完整進度"),
    ("approve", "<專案> <N> 放行"),
    ("reject", "<專案> <N> <理由> 打回"),
    ("retry", "<專案> <N> 輪數用盡後重置"),
    ("help", "指令說明"),
]

_HELP = ("<b>指令</b>\n" + "\n".join(
    f"/{name} — {html.escape(desc)}" for name, desc in _COMMAND_MENU) +
    "\n\n停下來的 pipeline 會直接附按鈕,多半不用打字。\n"
    "決策成功後會自動重啟 <code>run</code>。"
    "(<code>reset</code> 不可逆,只能在機器上下。)")

_NEEDS_PROJECT = frozenset({"status", "approve", "reject", "retry"})
_NEEDS_MILESTONE = frozenset({"approve", "reject", "retry"})
_NEEDS_REASON = frozenset({"reject"})
# **白名單**。動到 state 的 verb 就這三個,`reset` 刻意不在裡面。
_VERBS = frozenset({"guards", "help"}) | _NEEDS_PROJECT
# 決策成功之後要接著重啟 `run` 的(`status` 只是讀,不用)
_DECISIONS = _NEEDS_MILESTONE

# 按鈕 → verb。callback_data 有 64 bytes 上限,所以用單字母。
_CALLBACK_VERBS = {"a": "approve", "t": "retry", "r": "reject"}
# 「回覆這則訊息寫下理由」的目標藏在提示訊息的文字裡(Telegram 會把原訊息
# 一起回傳),所以要一個機器讀得回來的標記。專案名這一段吃「除了 `#` 以外的任何
# 字元」**不是**放寬驗證:白名單擋不住的是 CJK 或含 `&` 的檔名(那是合法的
# config stem),擋下來只會讓打回靜靜失效。真正的關卡在 `resolve_config` ——
# 它查 `guard.collect()` 那張表,對不到 stem 就是對不到,沒有拼路徑的餘地。
_TARGET_RE = re.compile(r"\[([^#]{1,64})#(\d{1,6})\]")


@dataclass
class Cmd:
    verb: str
    project: str | None = None
    milestone: int | None = None
    reason: str = ""


@dataclass
class Msg:
    """一則要送出去的訊息。把「送什麼」與「怎麼送」分開,`handle` 才測得到。"""
    text: str                                    # 已經是 HTML
    buttons: list[list[dict]] | None = None      # inline_keyboard
    force_reply: bool = False


# -- 純邏輯(不碰網路 / 檔案系統,所以測得到)--------------------------------

def parse_command(text: str) -> Cmd | None:
    """把一則訊息解成指令;不合法一律回 `None`(白名單,不是黑名單)。

    群組裡 Telegram 會送成 `/approve@my_bot`,所以 `@` 後面要切掉。
    reason 吃剩下的全部(含空白與換行)—— 打回的理由本來就會是一段話。
    """
    parts = (text or "").strip().split(maxsplit=3)
    if not parts or not parts[0].startswith("/"):
        return None

    verb = parts[0][1:].split("@", 1)[0].lower()
    if verb not in _VERBS:
        return None

    project = parts[1] if len(parts) > 1 else None
    if verb in _NEEDS_PROJECT and not project:
        return None

    milestone = None
    if verb in _NEEDS_MILESTONE:
        # `isdigit()` 順便擋掉 `-1` 與 `1.5`
        if len(parts) < 3 or not parts[2].isdigit():
            return None
        milestone = int(parts[2])

    reason = parts[3].strip() if len(parts) > 3 else ""
    if verb in _NEEDS_REASON and not reason:
        return None
    return Cmd(verb, project, milestone, reason)


def parse_callback(data: str) -> Cmd | None:
    """按鈕的 `callback_data` → 指令。**跟打字進來的訊息走一樣的嚴格度**。

    按鈕看起來是我們自己發的,但 `callback_data` 一樣是從網路回來的字串,
    沒有理由給它比較寬的路。
    """
    parts = (data or "").split(":")
    if len(parts) != 3:
        return None
    tag, project, num = parts
    verb = _CALLBACK_VERBS.get(tag)
    if not verb or not project or not num.isdigit():
        return None
    return Cmd(verb, project, int(num))


def callback_data(tag: str, project: str, milestone: int) -> str | None:
    """組 `callback_data`;超過 64 bytes 回 `None`(呼叫端就不放這顆按鈕)。

    超過的話 Telegram 直接回 400,整則訊息連同其他按鈕一起送不出去 ——
    寧可少一顆按鈕,人還有打字那條路。
    """
    data = f"{tag}:{project}:{milestone}"
    return data if len(data.encode("utf-8")) <= _CALLBACK_LIMIT else None


def authorized(update: dict, chat_id: str) -> bool:
    """chat id 白名單 —— 這是唯一的驗證,所以每一條退化路徑都要 fail-closed。

    **`chat_id` 是空字串時回 `False`**:那代表沒設定,而「沒設定」不能等於
    「放行所有人」。(`serve()` 也會在起飛前就擋掉,這裡是第二道。)

    **刻意只認 `message` 與 `callback_query`,不認 `edited_message`**:
    把一則舊訊息編輯成 `/approve` 不該觸發一次決策。
    """
    if not chat_id:
        return False
    cb = update.get("callback_query")
    msg = (cb or {}).get("message") if cb else update.get("message")
    return str(((msg or {}).get("chat") or {}).get("id", "")) == str(chat_id)


def message_text(update: dict) -> str:
    return ((update.get("message") or {}).get("text") or "")


def reply_target(update: dict) -> tuple[str, int] | None:
    """這則訊息是不是在回覆我們的「寫下理由」提示?是的話回 (專案, milestone)。"""
    quoted = (((update.get("message") or {}).get("reply_to_message") or {})
              .get("text") or "")
    # 先還原 HTML entity:`reject_prompt` 送出去的是 `_esc(project)`,含 `&`
    # 的專案名回來時是 `&amp;`,不還原就對不回 config 的 stem。
    m = _TARGET_RE.search(html.unescape(quoted))
    return (m.group(1), int(m.group(2))) if m else None


def resolve_config(project: str, rows: list[guard.GuardRow]) -> Path | None:
    """專案名 → config 路徑。**查表,不拼路徑** —— `../../etc` 對不到任何 stem。"""
    for row in rows:
        if row.config is not None and row.config.stem == project:
            return row.config
    return None


def escalations(rows: list[guard.GuardRow],
                already: set[tuple]) -> list[guard.GuardRow]:
    """挑出「停下來太久、還沒有人處理」的 pipeline。`already` 會被就地更新。

    這裡的時間判斷與 `guards` 的 ⚠ 不同,兩者不要混:`guards` 刻意**不**拿
    時間當「卡住」的代理(implement 階段跑一小時不寫存檔是正常的);這裡看的
    是**已經停下來等人之後**又過了多久 —— 停著不動就是真的沒人處理,沒有誤報
    的空間。

    去重的 key 帶 `await_reason`:同一個 milestone 先卡 merge_gate、被打回之後
    又卡 stuck,那是兩件事,要各推一次。
    """
    out = []
    for row in rows:
        if not row.parked or row.age_sec is None:
            continue
        if row.age_sec < row.escalate_after_min * 60:
            continue
        key = (row.project, row.milestone, row.await_reason, row.phase)
        if key in already:
            continue
        already.add(key)
        out.append(row)
    return out


# -- 排版 --------------------------------------------------------------------
#
# 用 HTML 不用 MarkdownV2:MarkdownV2 對沒跳脫的 `-` `.` `(` `#` 一律回 400,
# 而那些字元我們每則訊息都有(指令、檔名、金額)。HTML 只要跳脫 `& < >`,
# `html.escape()` 一個函式解決,不會有「訊息靜靜地送不出去」那種失敗模式。

def _esc(text) -> str:
    return html.escape(str(text), quote=False)


def render_row(row: guard.GuardRow) -> str:
    """一條 pipeline 的 HTML。手機是比例字體,所以**不排欄位**,用 `·` 分隔。"""
    who = _esc(row.project or row.session)
    if row.problem:
        return f"<b>{who}</b> · ⚠ {_esc(row.problem)}"
    if not row.started:
        return f"<b>{who}</b> · 尚未開跑"

    if row.parked:
        head = f"<b>{who}</b> · M{row.milestone} ⏸ 等待決策"
        bits = [_esc(row.await_reason or row.phase)]
    else:
        head = f"<b>{who}</b> · M{row.milestone} {_esc(row.phase)}"
        bits = []
        if row.review_round:
            bits.append(f"round={row.review_round}")
    if row.pr_number:
        bits.append(f"PR#{row.pr_number}")
    if row.cost_usd:
        bits.append(f"~${row.cost_usd:.2f}")
    bits.append(_esc(row.age))
    if row.parked and not row.alive:
        bits.append("⚠ 沒有守護 agent")
    return f"{head}\n<i>{' · '.join(bits)}</i>"


def row_buttons(row: guard.GuardRow, pr_url: str | None = None
                ) -> list[list[dict]] | None:
    """停下來的 pipeline 給的操作按鈕。沒停下來就不給(沒有東西好按)。

    第一顆按 `resume_verb` 決定:`stuck` 給「續跑」(retry),`await_human`
    才給「放行」(approve)—— `approve` 吃不下 `stuck`,擺錯的按鈕保證失敗。
    """
    if not row.parked or not row.config:
        return None
    first = ("✅ 放行" if row.resume_verb == "approve" else "▶️ 續跑")
    tag = "a" if row.resume_verb == "approve" else "t"

    top: list[dict] = []
    for label, t in ((first, tag), ("❌ 打回", "r")):
        data = callback_data(t, row.project, row.milestone)
        if data:
            top.append({"text": label, "callback_data": data})
    rows = [top] if top else []
    if pr_url and row.pr_number:
        rows.append([{"text": f"🔗 PR#{row.pr_number}", "url": pr_url}])
    return rows or None


def _pr_url(row: guard.GuardRow) -> str | None:
    """問 `gh` 拿 PR 網址。問不到就回 `None`(少一顆按鈕,不是錯誤)。"""
    if not row.pr_number or not row.repo:
        return None
    try:
        return Gh(row.repo).pr_view(row.pr_number, "url").get("url")
    except Exception as e:  # noqa: BLE001 - 少一顆按鈕不該讓整則訊息送不出去
        log.warning("拿不到 PR#%s 的網址:%s", row.pr_number, e)
        return None


def reject_prompt(project: str, milestone: int) -> Msg:
    """打回的理由用 ForceReply 收 —— 比要人打一整串 `/reject x 6 …` 好按太多。

    目標藏在文字裡的 `[專案#N]`,因為 Telegram 回覆時會把原訊息一起帶回來,
    那是唯一不用自己記狀態就能把理由接回目標的方法(bot 重啟也不會忘)。
    """
    return Msg(
        f"❌ 打回 <b>{_esc(project)}</b> M{milestone} —— "
        f"直接<b>回覆這則訊息</b>寫下理由(會交給 implementer):\n"
        f"<code>[{_esc(project)}#{milestone}]</code>",
        force_reply=True)


# -- 外部動作 ----------------------------------------------------------------

def run_verb(cfg_path: Path, verb: str, milestone: int | None = None,
             reason: str = "") -> tuple[int, str]:
    """跑一個既有的 CLI verb,回傳 (exit code, 輸出)。

    **argv list,不走 shell** —— reason 是使用者自由文字,經過 shell 就等於
    給了一個任意命令執行的入口。
    """
    argv = [sys.executable, "-m", "milestone_pipeline", verb,
            "--config", cfg_path.name]
    if milestone is not None:
        argv += ["--milestone", str(milestone)]
    if reason:
        argv += ["--reason", reason]
    try:
        p = subprocess.run(argv, cwd=cfg_path.parent, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"執行 {verb} 失敗:{e}"
    return p.returncode, (p.stdout + p.stderr).strip()


def restart_run(cfg_path: Path) -> str:
    """決策完把 `run` 重新起飛(detached),回傳 log 檔路徑。

    不重啟的話 approve 完 pipeline 還是停在那裡 —— 等於沒真的從手機推動它。
    起法同 `formosa.yaml` 檔頭那串:`stdin` 接 DEVNULL、輸出附加到
    `~/pipeline-<stem>.log`。已經有一個 `run` 在跑時 `lock.exclusive()` 會
    擋下來並寫進那個 log,那是預期內的。
    """
    log_path = Path.home() / f"pipeline-{cfg_path.stem}.log"
    argv = [sys.executable, "-m", "milestone_pipeline", "run",
            "--config", cfg_path.name]
    with open(log_path, "a", encoding="utf-8") as fh:
        subprocess.Popen(argv, cwd=cfg_path.parent, stdin=subprocess.DEVNULL,
                         stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return str(log_path)


def do_command(cmd: Cmd, workdir: Path) -> list[Msg]:
    """執行一個已經解析好的指令 → 要回覆的訊息。打字與按鈕共用這一段。"""
    if cmd.verb == "help":
        return [Msg(_HELP)]

    rows = guard.collect(workdir)
    if cmd.verb == "guards":
        return render_guards(rows)

    cfg_path = resolve_config(cmd.project, rows)
    if cfg_path is None:
        known = ", ".join(sorted(r.config.stem for r in rows if r.config))
        return [Msg(f"找不到專案 <code>{_esc(cmd.project)}</code>。"
                    f"目前有:{_esc(known) or '(沒有)'}")]

    # 打回但還沒有理由 → 先問理由(按鈕來的一定走這條)
    if cmd.verb == "reject" and not cmd.reason:
        return [reject_prompt(cmd.project, cmd.milestone)]

    rc, out = run_verb(cfg_path, cmd.verb, cmd.milestone, cmd.reason)
    body = _esc(out) or f"(沒有輸出,exit={rc})"
    # `status` 停下來時本來就回 1,那不是失敗;它的輸出是對齊的表格,用 <pre>
    if cmd.verb == "status":
        return [Msg(f"<pre>{body}</pre>")]
    if rc != 0:
        return [Msg(f"⚠ {body}")]
    try:
        return [Msg(f"✅ {body}\n\n<i>已重啟 run:"
                    f"<code>{_esc(restart_run(cfg_path))}</code></i>")]
    except OSError as e:
        return [Msg(f"✅ {body}\n\n⚠ 重啟 run 失敗:{_esc(e)}")]


def render_guards(rows: list[guard.GuardRow]) -> list[Msg]:
    """`/guards` 的輸出:先一則總覽,停下來的每條各自一則(才帶得了按鈕)。

    Telegram 一則訊息只能掛一組 inline keyboard,所以停下來的不能跟總覽混在
    一起 —— 兩條同時停的話你會分不出按鈕是哪一條的。
    """
    if not rows:
        return [Msg("沒有找到任何 pipeline config。")]
    quiet = [r for r in rows if not r.parked]
    out = []
    if quiet:
        out.append(Msg(f"\n{_SEP}\n".join(render_row(r) for r in quiet)))
    for row in rows:
        if row.parked:
            out.append(Msg(render_row(row), buttons=row_buttons(row, _pr_url(row))))
    return out


def handle(update: dict, workdir: Path) -> list[Msg]:
    """一則 update → 要回覆的訊息。呼叫端已經驗過 chat id。"""
    text = message_text(update)
    if not text.strip():
        return []

    # 在回覆「寫下理由」的提示 → 這整段文字就是打回的理由
    target = reply_target(update)
    if target and not text.startswith("/"):
        project, milestone = target
        return do_command(Cmd("reject", project, milestone, text.strip()), workdir)

    cmd = parse_command(text)
    if cmd is None:
        return [Msg(f"不認得這個指令。\n\n{_HELP}")]
    return do_command(cmd, workdir)


# -- Telegram 傳輸 -----------------------------------------------------------
#
# 錯誤訊息**一律不帶 URL** —— bot token 就在 URL 的路徑裡,而 log 是會被貼給
# 別人看的東西(同 notify.WebhookNotifier 刻意不帶 URL 的理由)。

def _api(token: str, method: str, payload: dict) -> dict | None:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("%s 失敗:%s", method, type(e).__name__)
        return None


def send(token: str, chat_id: str, msg: Msg) -> int | None:
    """送一則訊息,回傳 message_id(送失敗回 None)。"""
    payload: dict = {
        "chat_id": chat_id,
        "text": msg.text[:_TG_LIMIT],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if msg.buttons:
        payload["reply_markup"] = {"inline_keyboard": msg.buttons}
    elif msg.force_reply:
        payload["reply_markup"] = {"force_reply": True, "selective": True}
    data = _api(token, "sendMessage", payload)
    return ((data or {}).get("result") or {}).get("message_id")


def edit_message(token: str, chat_id: str, message_id: int, text: str) -> None:
    """把原訊息改掉並**收走按鈕** —— 同一顆放行按不了第二次。"""
    _api(token, "editMessageText", {
        "chat_id": chat_id, "message_id": message_id,
        "text": text[:_TG_LIMIT], "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    """一定要回,不然手機上那顆按鈕會一直轉。"""
    _api(token, "answerCallbackQuery",
         {"callback_query_id": callback_id, "text": text[:200]})


def set_my_commands(token: str) -> None:
    _api(token, "setMyCommands", {"commands": [
        {"command": name, "description": desc} for name, desc in _COMMAND_MENU]})


def poll_once(token: str, offset: int, timeout: int = 50) -> tuple[list[dict], int]:
    """long-poll 一次 `getUpdates`,回傳 (updates, 下一個 offset)。

    連線錯誤只記 log 並回原本的 offset,下一圈再試 —— 同「通知失敗永遠不能
    中斷 pipeline」的慣例,一次網路抖動不該讓 bot 掛掉。
    """
    query = urllib.parse.urlencode({
        "timeout": timeout,
        "offset": offset,
        # edited_message 不收:編輯舊訊息不該觸發決策(同 `authorized`)
        "allowed_updates": json.dumps(["message", "callback_query"]),
    })
    url = f"https://api.telegram.org/bot{token}/getUpdates?{query}"
    try:
        with urllib.request.urlopen(url, timeout=timeout + 15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("getUpdates 失敗(%s),下一圈再試", type(e).__name__)
        return [], offset

    updates = data.get("result") or []
    for u in updates:
        offset = max(offset, int(u.get("update_id", 0)) + 1)
    return updates, offset


def _load_offset(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _handle_callback(update: dict, token: str, chat_id: str,
                     workdir: Path) -> None:
    """按鈕。先回 answerCallbackQuery(不然一直轉),再把原訊息改掉。"""
    cb = update["callback_query"]
    answer_callback(token, cb.get("id", ""))
    cmd = parse_callback(cb.get("data", ""))
    if cmd is None:
        send(token, chat_id, Msg("認不得這顆按鈕(可能是舊訊息)。"))
        return

    msgs = do_command(cmd, workdir)
    src = (cb.get("message") or {}).get("message_id")
    # 第一則直接蓋掉原訊息:按鈕跟著消失,同一顆放行按不了第二次
    if src and msgs:
        edit_message(token, chat_id, src, msgs[0].text)
        msgs = msgs[1:]
    for m in msgs:
        send(token, chat_id, m)


def serve(workdir: Path) -> int:
    """主迴圈。前景跑;要在背景就用 `nohup … &`(同 orchestrator 的起法)。

    **token 與 chat id 只吃環境變數**:bot 是跨專案的,不該挑某一份 config 的
    設定來用;而這兩個值本來就規定走 `TELEGRAM_BOT_TOKEN` /
    `TELEGRAM_CHAT_ID`(token 是密鑰,chat id 是永久的個人識別碼,這個 repo
    是公開的)。
    """
    token = os.environ.get(TELEGRAM_TOKEN_ENV, "")
    chat_id = os.environ.get(TELEGRAM_CHAT_ID_ENV, "")
    if not token or not chat_id:
        # **fail-closed,與 config.py 那邊「缺了只警告」刻意相反。** 那裡缺的
        # 後果是這台收不到通知;這裡缺 chat id 的後果是誰都能下 approve。
        raise SystemExit(
            f"{TELEGRAM_TOKEN_ENV} 與 {TELEGRAM_CHAT_ID_ENV} 兩個都要設。"
            "chat id 是這個 bot 唯一的存取控制 —— 沒有它等於對所有人開放,"
            "所以這裡不降級。")

    workdir = workdir.resolve()
    offset_path = workdir / OFFSET_FILE
    offset = _load_offset(offset_path)
    escalated: set[tuple] = set()
    log.info("Telegram 控制端點已上線(目錄 %s,offset %d)", workdir, offset)
    set_my_commands(token)
    send(token, chat_id, Msg(f"🤖 milestone-pipeline 控制端點上線。\n\n{_HELP}"))

    while True:
        updates, offset = poll_once(token, offset)
        if updates:
            # **先存 offset 再處理**:處理到一半 crash 的話,重啟後不能把
            # `/approve` 再吃一次。
            try:
                offset_path.write_text(str(offset), encoding="utf-8")
            except OSError as e:
                log.warning("存不了 offset(%s)—— 重啟後可能重播指令", e)
        for u in updates:
            if not authorized(u, chat_id):
                log.warning("丟掉一則不在白名單的訊息(update_id=%s)",
                            u.get("update_id"))
                continue
            if "callback_query" in u:
                _handle_callback(u, token, chat_id, workdir)
                continue
            for m in handle(u, workdir):
                send(token, chat_id, m)

        # watchdog:停下來太久還沒人處理才推。`notify.reasons` 把 merge_gate
        # 排掉之後,這是唯一還會告訴你「守護 agent 沒把它處理掉」的東西。
        for row in escalations(guard.collect(workdir), escalated):
            log.warning("升級推播:%s M%s 卡在 %s 已 %s",
                        row.project, row.milestone, row.await_reason, row.age)
            send(token, chat_id, Msg(
                f"🔔 <b>卡住了</b>\n\n{render_row(row)}",
                buttons=row_buttons(row, _pr_url(row))))

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
  3. **不走 shell、不拼路徑。** 一律 argv list,專案名只能對到
     `guard.collect()` 掃出來的那份清單(查表)。
  4. 只能下既有的 CLI verb,**沒有 `/reset`** —— 不可逆的清除不該掛在一個
     手機打字打錯就會觸發的介面上。

bot 自己**沒有寫入能力**,這點跟守護 agent 一樣;差別在守護 agent 靠
`--disallowed-tools` 擋,bot 靠「只有這幾個 subprocess 跑得起來」擋。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import guard
from .config import TELEGRAM_CHAT_ID_ENV, TELEGRAM_TOKEN_ENV

log = logging.getLogger("pipeline")

# sendMessage 上限 4096,留餘裕(同 notify.TelegramNotifier)
_TG_LIMIT = 4000

# 進度存到哪。**一定要落地**:不存的話 bot 重啟會把上一批 update 再吃一次,
# 而重播的是 `/approve` —— 那是真的會動到 state 的東西。
# 檔名進 `.gitignore`:它落在 orchestrator 自己的 repo 裡,而
# `guard.repo_warnings()` 看的就是 `git status --porcelain`,不排掉的話每次
# 起守護 agent 都會警告「有未 commit 的變動」。
OFFSET_FILE = ".mp-tgbot-offset"

_HELP = """指令:
/guards — 全部 pipeline 的狀態
/status <專案> — 單一 pipeline 的完整進度
/approve <專案> <N> — 放行停在決策點的 milestone
/reject <專案> <N> <理由> — 打回,理由交給 implementer
/retry <專案> <N> — 輪數用盡後重置輪數
決策成功後會自動重啟 run。(`reset` 不可逆,只能在機器上下。)"""

_NEEDS_PROJECT = frozenset({"status", "approve", "reject", "retry"})
_NEEDS_MILESTONE = frozenset({"approve", "reject", "retry"})
_NEEDS_REASON = frozenset({"reject"})
# **白名單**。動到 state 的 verb 就這三個,`reset` 刻意不在裡面。
_VERBS = frozenset({"guards", "help"}) | _NEEDS_PROJECT
# 決策成功之後要接著重啟 `run` 的(`status` 只是讀,不用)
_DECISIONS = _NEEDS_MILESTONE


@dataclass
class Cmd:
    verb: str
    project: str | None = None
    milestone: int | None = None
    reason: str = ""


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


def authorized(update: dict, chat_id: str) -> bool:
    """chat id 白名單 —— 這是唯一的驗證,所以每一條退化路徑都要 fail-closed。

    **`chat_id` 是空字串時回 `False`**:那代表沒設定,而「沒設定」不能等於
    「放行所有人」。(`serve()` 也會在起飛前就擋掉,這裡是第二道。)

    **刻意只認 `message`,不認 `edited_message`**:把一則舊訊息編輯成
    `/approve` 不該觸發一次決策。
    """
    if not chat_id:
        return False
    msg = update.get("message") or {}
    return str((msg.get("chat") or {}).get("id", "")) == str(chat_id)


def message_text(update: dict) -> str:
    return ((update.get("message") or {}).get("text") or "")


def resolve_config(project: str, rows: list[guard.GuardRow]) -> Path | None:
    """專案名 → config 路徑。**查表,不拼路徑** —— `../../etc` 對不到任何 stem。"""
    for row in rows:
        if row.config is not None and row.config.stem == project:
            return row.config
    return None


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


def handle(text: str, workdir: Path) -> str:
    """一則訊息 → 要回覆的文字(空字串 = 不回)。呼叫端已經驗過 chat id。"""
    if not text.strip():
        return ""
    cmd = parse_command(text)
    if cmd is None:
        return f"不認得這個指令。\n\n{_HELP}"
    if cmd.verb == "help":
        return _HELP

    rows = guard.collect(workdir)
    if cmd.verb == "guards":
        body = "\n".join(ln for r in rows for ln in r.lines)
        return body or "沒有找到任何 pipeline config。"

    cfg_path = resolve_config(cmd.project, rows)
    if cfg_path is None:
        known = ", ".join(sorted(r.config.stem for r in rows if r.config))
        return f"找不到專案 `{cmd.project}`。目前有:{known or '(沒有)'}"

    rc, out = run_verb(cfg_path, cmd.verb, cmd.milestone, cmd.reason)
    out = out or f"(沒有輸出,exit={rc})"
    # `status` 停下來時本來就回 1,那不是失敗
    if cmd.verb not in _DECISIONS or rc != 0:
        return out
    try:
        return f"{out}\n\n已重啟 run,log:{restart_run(cfg_path)}"
    except OSError as e:
        return f"{out}\n\n⚠ 重啟 run 失敗:{e}"


# -- Telegram 傳輸 -----------------------------------------------------------
#
# 錯誤訊息**一律不帶 URL** —— bot token 就在 URL 的路徑裡,而 log 是會被貼給
# 別人看的東西(同 notify.WebhookNotifier 刻意不帶 URL 的理由)。

def send(token: str, chat_id: str, text: str) -> None:
    """送一則純文字。**不設 `parse_mode`** —— 同 notify:我們的內容每則都有
    `-` `.` `(` 這些字元,MarkdownV2 沒跳脫會直接 400,變成「通知靜靜地送不出去」。
    """
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:_TG_LIMIT],
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except (urllib.error.URLError, OSError) as e:
        log.warning("sendMessage 失敗:%s", type(e).__name__)


def poll_once(token: str, offset: int, timeout: int = 50) -> tuple[list[dict], int]:
    """long-poll 一次 `getUpdates`,回傳 (updates, 下一個 offset)。

    連線錯誤只記 log 並回原本的 offset,下一圈再試 —— 同「通知失敗永遠不能
    中斷 pipeline」的慣例,一次網路抖動不該讓 bot 掛掉。
    """
    query = urllib.parse.urlencode({
        "timeout": timeout,
        "offset": offset,
        # 只要 message:回報 edited_message / callback 只會多出要濾的東西
        "allowed_updates": json.dumps(["message"]),
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
    log.info("Telegram 控制端點已上線(目錄 %s,offset %d)", workdir, offset)
    send(token, chat_id, f"🤖 milestone-pipeline 控制端點上線。\n\n{_HELP}")

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
            reply = handle(message_text(u), workdir)
            if reply:
                send(token, chat_id, reply)

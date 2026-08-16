"""決策通知:pipeline 停下來等人時,把「發生什麼事、怎麼恢復」送到人看得到的地方。

設計與 reviewer 同形:抽象介面 + 多個實作,channel 名稱在 config 載入時就驗證。

一個硬規則:**通知失敗永遠不該讓 pipeline 掛掉**。所有實作的例外都由
MultiNotifier 吞掉並記 log —— 通知只是旁路,主流程的狀態已經存檔了,
人就算沒收到推播,`status` 指令也看得到。
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .gh import Gh

log = logging.getLogger("pipeline")

# 停下來等人的原因(同時是 MilestoneState.await_reason 的值)
R_MERGE_GATE = "merge_gate"       # 設定要求 merge 前人工放行
R_UNRESOLVED = "unresolved"       # implementer 回報與 reviewer 有未解決分歧
R_AGENT_ERROR = "agent_error"     # agent 回報錯誤(超預算 / 超輪數 / API 失敗)
R_STUCK = "stuck"                 # review 輪數用盡仍未 approve

_REASON_LABEL = {
    R_MERGE_GATE: "merge gate —— 此 milestone 設定為 merge 前需人工放行",
    R_UNRESOLVED: "implementer 回報與 reviewer 有未解決的分歧",
    R_AGENT_ERROR: "agent 回報錯誤(可能超過 max_turns / max_budget_usd,或 API 失敗)",
    R_STUCK: "review 輪數已用盡,仍未取得 approve",
}


@dataclass
class Decision:
    """一次「需要人決策」的事件。內容要能讓人在手機上就判斷,不必開電腦。"""

    milestone_index: int
    milestone_title: str
    reason: str
    detail: str                     # reviewer 結論 / 分歧內容 / 錯誤輸出
    config_hint: str = "pipeline.yaml"
    pr_number: int | None = None
    pr_url: str | None = None
    cost_usd: float = 0.0

    @property
    def title(self) -> str:
        return (f"⏸ Milestone {self.milestone_index}"
                f"({self.milestone_title})等待決策")

    @property
    def approve_cmd(self) -> str:
        return (f"python -m milestone_pipeline approve "
                f"--milestone {self.milestone_index} --config {self.config_hint}")

    @property
    def reject_cmd(self) -> str:
        return (f'python -m milestone_pipeline reject '
                f'--milestone {self.milestone_index} --reason "..." '
                f'--config {self.config_hint}')

    def body(self, *, mention: str = "") -> str:
        """給人看的 markdown。刻意把恢復指令放最後,方便直接複製。"""
        head = f"{mention} " if mention else ""
        lines = [
            f"## {self.title}",
            "",
            f"{head}**原因**:{_REASON_LABEL.get(self.reason, self.reason)}",
        ]
        if self.pr_url:
            lines.append(f"**PR**:{self.pr_url}")
        elif self.pr_number is not None:
            lines.append(f"**PR**:#{self.pr_number}")
        if self.cost_usd:
            lines.append(f"**本 milestone 累計花費**:~${self.cost_usd:.2f}")
        if self.detail.strip():
            lines += ["", "---", "", self.detail.strip()]
        lines += [
            "",
            "---",
            "",
            "```bash",
            f"# 放行(繼續 merge)",
            self.approve_cmd,
            "",
            f"# 打回(把 reason 當成新一輪意見交給 implementer)",
            self.reject_cmd,
            "```",
        ]
        return "\n".join(lines)

    def plain(self) -> str:
        """給不吃 markdown 的通道(桌面通知)用的精簡版。"""
        pr = f" / PR #{self.pr_number}" if self.pr_number is not None else ""
        return (f"{self.title}{pr}\n"
                f"{_REASON_LABEL.get(self.reason, self.reason)}\n"
                f"放行:{self.approve_cmd}")


class Notifier(ABC):
    @abstractmethod
    def notify(self, decision: Decision) -> None: ...


class NullNotifier(Notifier):
    """不通知(仍會由 orchestrator 記 log)。"""

    def notify(self, decision: Decision) -> None:
        return


class PrCommentNotifier(Notifier):
    """貼到 PR 上。決策紀錄天然留在 PR,事後可追;@mention 會走 GitHub 推播。

    沒有 PR 編號時(例如實作階段就爆掉)無處可貼,直接跳過。
    """

    def __init__(self, repo_path: Path, mention: str = ""):
        self.gh = Gh(repo_path)
        self.mention = mention

    def notify(self, decision: Decision) -> None:
        if decision.pr_number is None:
            log.warning("PrCommentNotifier:milestone %d 還沒有 PR,略過通知。",
                        decision.milestone_index)
            return
        self.gh.pr_comment(decision.pr_number, decision.body(mention=self.mention))


class WebhookNotifier(Notifier):
    """推到 Discord / Slack incoming webhook。用 stdlib,不加依賴。"""

    # Discord 的 content 上限 2000 字元,超過會 400
    _DISCORD_LIMIT = 1900

    def __init__(self, url: str, fmt: str = "discord", mention: str = ""):
        self.url = url
        self.fmt = fmt
        self.mention = mention

    def _payload(self, decision: Decision) -> dict:
        body = decision.body(mention=self.mention)
        if self.fmt == "discord":
            return {"content": body[:self._DISCORD_LIMIT]}
        if self.fmt == "slack":
            return {"text": body}
        return {"title": decision.title, "body": body,
                "reason": decision.reason,
                "milestone": decision.milestone_index,
                "pr_number": decision.pr_number}

    def notify(self, decision: Decision) -> None:
        data = json.dumps(self._payload(decision)).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 300:
                    log.warning("webhook 回應 %s", resp.status)
        except urllib.error.HTTPError as e:
            # 不要把 webhook URL 印進 log(裡面含 token)
            raise RuntimeError(f"webhook HTTP {e.code}") from None


class TelegramNotifier(WebhookNotifier):
    """推到 Telegram bot。無人值守跑時最實用的一個 channel:人不在電腦前也收得到。

    沿用 `WebhookNotifier` 的 POST 不只是省行數 —— 它的 `HTTPError` 處理
    **刻意不把 URL 帶進錯誤訊息**,而 bot token 就在 URL 的路徑裡,
    自己寫一份很容易把 token 漏進 log。

    **不設 `parse_mode`。** `Decision.body()` 是 markdown,而 Telegram 的
    MarkdownV2 會對沒跳脫的 `-` `.` `(` `#` 一律回 400 —— 那些字元我們每則
    通知都有(指令、檔名、標題)。純文字送出去照樣看得懂,而且不會有一種
    「通知靜靜地送不出去」的失敗模式。
    """

    # sendMessage 上限 4096 字元,留餘裕
    _TG_LIMIT = 4000

    def __init__(self, token: str, chat_id: str, mention: str = ""):
        super().__init__(f"https://api.telegram.org/bot{token}/sendMessage",
                         fmt="telegram", mention=mention)
        self.chat_id = chat_id

    def _payload(self, decision: Decision) -> dict:
        return {
            "chat_id": self.chat_id,
            "text": decision.body(mention=self.mention)[:self._TG_LIMIT],
            "disable_web_page_preview": True,
        }


def _applescript_str(text: str) -> str:
    """把任意文字包成 AppleScript 的字串常值(含前後引號)。

    順序不能顛倒:反斜線一定要先跳脫,否則後面補進去的 `\\"` 會被再跳脫一次。
    AppleScript 的字串常值不能含真正的換行,所以換成 `\\n` 跳脫序列。
    """
    body = (text.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n"))
    return f'"{body}"'


class DesktopNotifier(Notifier):
    """桌面氣泡通知。人在電腦前時最即時,人不在就等於沒通知,當補充用。

    **同一份 config 會在兩台機器上跑**(這條 pipeline 在 milestone 之間換過
    機器,見 `formosa.yaml` 檔頭),所以這裡按平台分支而不是要人改 config ——
    否則 mac 上每次 park 都會噴 `No such file or directory: 'powershell'`。
    不認得的平台沒有共通做法,記個 log 就算了(通知本來就是旁路)。
    """

    def notify(self, decision: Decision) -> None:
        if sys.platform == "darwin":
            self._darwin(decision)
        elif sys.platform == "win32":
            self._win32(decision)
        else:
            log.warning("DesktopNotifier:平台 %s 沒有桌面通知實作,略過。",
                        sys.platform)

    def _darwin(self, decision: Decision) -> None:
        """macOS:osascript。不經 shell,所以只要處理 AppleScript 自己的跳脫。

        **從 ssh 起的行程看不到通知中心**:rc 是 0,stderr 只有一行
        `NSNotificationCenter connection invalid`,然後什麼都不會跳出來 ——
        因為那個行程不在 GUI(Aqua)session 裡。`launchctl asuser` 可以送進去,
        但它要 root,不適合放在這裡。所以無人值守跑(orchestrator 由 ssh 起)
        時 desktop 這個 channel 等於沒有,要靠 `pr_comment` / `webhook`;
        在 mac 上直接開終端機跑才收得到。這是平台限制,不是這裡的 bug。
        """
        script = (f"display notification {_applescript_str(decision.plain())} "
                  f'with title "milestone-pipeline"')
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, timeout=60)

    def _win32(self, decision: Decision) -> None:
        """Windows:用 -EncodedCommand(UTF-16LE base64)餵 PowerShell。

        這台機器的 console 預設是 cp950,直接用 -Command 傳中文會被吃掉。
        """
        text = decision.plain().replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information;"
            "$n.Visible = $true;"
            f"$n.ShowBalloonTip(20000, 'milestone-pipeline', '{text}', "
            "[System.Windows.Forms.ToolTipIcon]::Warning);"
            "Start-Sleep -Seconds 12; $n.Dispose()"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=60,
        )


class MultiNotifier(Notifier):
    """依序送到所有 channel。單一 channel 失敗不影響其他,也不影響主流程。"""

    def __init__(self, children: list[Notifier],
                 reasons: list[str] | None = None):
        self.children = children
        # 哪些 park 原因值得吵人。`None` = 全部(舊行為)。**過濾在這裡而不是
        # 在每個 channel 裡**:語意是「這件事值不值得推播」,與管道無關。
        # 被濾掉的仍然照常存檔、照常寫 log —— 少的只有推播。
        self.reasons = set(reasons) if reasons is not None else None

    def notify(self, decision: Decision) -> None:
        if self.reasons is not None and decision.reason not in self.reasons:
            log.info("park 原因 %s 不在 notify.reasons 裡,不推播"
                     "(狀態已存檔,`status` / `guards` 看得到)。", decision.reason)
            return
        for child in self.children:
            try:
                child.notify(decision)
            except Exception as e:  # noqa: BLE001 - 通知失敗絕不能中斷 pipeline
                log.warning("通知失敗(%s):%s", type(child).__name__, e)


def make_notifier(cfg, repo_path: Path) -> Notifier:
    """依 config 組出 notifier。channel 名稱已在 config 載入時驗證過。"""
    children: list[Notifier] = []
    for ch in cfg.channels:
        if ch == "pr_comment":
            children.append(PrCommentNotifier(repo_path, cfg.mention))
        elif ch == "webhook":
            children.append(
                WebhookNotifier(cfg.webhook_url, cfg.webhook_format, cfg.mention))
        elif ch == "telegram":
            missing = [name for name, val in
                       (("token(notify.telegram_token 或環境變數 "
                         "TELEGRAM_BOT_TOKEN)", cfg.telegram_token),
                        ("chat id(notify.telegram_chat_id 或環境變數 "
                         "TELEGRAM_CHAT_ID)", cfg.telegram_chat_id))
                       if not val]
            if missing:
                # 缺任一個就整個不裝這個 channel(不是裝了之後每次 park 都 400/401)。
                # 這是降級不是錯誤 —— 同 OCR 沒裝、desktop 平台不支援的處理方式。
                log.warning("notify.channels 含 telegram,但沒有 %s,這次跑不送 "
                            "telegram 通知。", "、也沒有 ".join(missing))
                continue
            children.append(
                TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id,
                                 cfg.mention))
        elif ch == "desktop":
            children.append(DesktopNotifier())
    if not children:
        return NullNotifier()
    return MultiNotifier(children, getattr(cfg, "reasons", None))

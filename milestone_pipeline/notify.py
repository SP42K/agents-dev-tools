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


class DesktopNotifier(Notifier):
    """Windows 氣泡通知。人在電腦前時最即時,人不在就等於沒通知,當補充用。

    用 -EncodedCommand(UTF-16LE base64)餵 PowerShell:這台機器的 console
    預設是 cp950,直接用 -Command 傳中文會被吃掉。
    """

    def notify(self, decision: Decision) -> None:
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

    def __init__(self, children: list[Notifier]):
        self.children = children

    def notify(self, decision: Decision) -> None:
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
        elif ch == "desktop":
            children.append(DesktopNotifier())
    if not children:
        return NullNotifier()
    return MultiNotifier(children)

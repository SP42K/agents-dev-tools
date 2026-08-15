"""載入與驗證 pipeline.yaml。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# SDK 允許的 permission_mode(claude_agent_sdk.types.PermissionMode)
PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
)
MERGE_METHODS = frozenset({"squash", "merge", "rebase"})
# agent 跑在哪個 runtime 上(見 backend.py)。目前只有一個實作。
BACKENDS = frozenset({"claude"})
REVIEWER_TYPES = frozenset({"script", "actions", "hybrid"})
# merge 前是否需要人工放行
MERGE_GATES = frozenset({"auto", "ask"})
NOTIFY_CHANNELS = frozenset({"pr_comment", "webhook", "desktop"})
WEBHOOK_FORMATS = frozenset({"discord", "slack", "raw"})


def _one_of(value: str, allowed: frozenset[str], field_name: str) -> str:
    if value not in allowed:
        raise SystemExit(
            f"{field_name} 不合法: {value!r}(可用: {', '.join(sorted(allowed))})"
        )
    return value


@dataclass
class AgentCfg:
    model: str
    permission_mode: str = "acceptEdits"
    max_turns: int = 80
    max_budget_usd: float | None = None


@dataclass
class ReviewerCfg(AgentCfg):
    type: str = "script"  # "script" | "actions" | "hybrid"
    # type="actions" 用
    poll_interval_sec: int = 60
    poll_timeout_sec: int = 1800
    # type="hybrid" 用(open-code-review 的委託模式)。
    # 委託模式不呼叫 LLM,所以這裡沒有任何模型 / 金鑰 / token 預算設定。
    ocr_exe: str = "ocr"
    ocr_timeout_sec: int = 300     # 單次 ocr 行程的 subprocess timeout
    ocr_exclude: str = ""          # --exclude,逗號分隔的 gitignore 樣式
    ocr_rule_path: str = ""        # --rule,自訂規則 JSON
    ocr_max_rule_chars: int = 40000  # 塞進 prompt 的規則字數上限,超過就截斷並註明


@dataclass
class LoopCfg:
    max_review_rounds: int = 5
    compact_between_rounds: bool = True
    merge_method: str = "squash"
    # merge 前的人工關卡:auto = 直接 merge;ask = 停下來等 approve 指令
    merge_gate: str = "auto"
    # merge_gate=ask 時,從第幾個 milestone 開始才需要人工放行
    # (前期骨架可以全自動,越後面越該人看一眼)
    merge_gate_from_milestone: int = 1
    # implementer 回報與 reviewer 有未解決分歧時,是否停下來問人
    gate_on_unresolved: bool = True
    # agent 回報錯誤(超預算 / 超輪數 / API 失敗)時停下來問人,而不是直接中止
    gate_on_agent_error: bool = True
    # reviewer approve 之後、merge 之前跑的確定性驗收命令(走 shell,
    # 所以 `pytest && ruff check .` 這種寫法可以)。空字串 = 不跑,維持舊行為。
    # 失敗時共用既有的 max_review_rounds,把輸出當這輪的意見交回 implementer。
    verify_command: str = ""
    verify_timeout_sec: int = 900

    def needs_human_merge(self, milestone_index: int) -> bool:
        """merge 前是否要停下來等人。純函式,方便單獨測試。"""
        if self.merge_gate != "ask":
            return False
        return milestone_index >= self.merge_gate_from_milestone


@dataclass
class NotifyCfg:
    channels: list[str] = field(default_factory=list)
    mention: str = ""            # 例如 "@SP42K",會放在通知開頭觸發推播
    webhook_url: str = ""
    webhook_format: str = "discord"


@dataclass
class Config:
    repo_path: Path
    base_branch: str
    remote: str
    plan_path: Path
    implementer: AgentCfg
    reviewer: ReviewerCfg
    loop: LoopCfg = field(default_factory=LoopCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)
    backend: str = "claude"     # 兩個 agent 共用同一個 runtime
    state_file: Path = Path(".pipeline-state.json")
    # 只用來組出給人複製的恢復指令,不影響流程
    config_hint: str = "pipeline.yaml"

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

        repo_path = Path(raw["repo"]["path"]).expanduser().resolve()
        if not repo_path.is_dir():
            raise SystemExit(f"repo.path 不存在: {repo_path}")

        def _resolve(p: str) -> Path:
            pp = Path(p).expanduser()
            return pp if pp.is_absolute() else repo_path / pp

        imp = raw["implementer"]
        rev = raw["reviewer"]
        loop = raw.get("loop", {})
        noti = raw.get("notify", {}) or {}

        channels = noti.get("channels", []) or []
        if isinstance(channels, str):       # 容忍 `channels: pr_comment` 的寫法
            channels = [channels]
        for ch in channels:
            _one_of(ch, NOTIFY_CHANNELS, "notify.channels")
        webhook_url = noti.get("webhook_url", "") or ""
        if "webhook" in channels and not webhook_url:
            # 設定錯誤在載入時就炸,不要等到流程跑一小時後才發現通知送不出去
            raise SystemExit("notify.channels 含 webhook,但沒有設定 notify.webhook_url")

        return cls(
            repo_path=repo_path,
            base_branch=raw["repo"].get("base_branch", "main"),
            remote=raw["repo"].get("remote", "origin"),
            plan_path=_resolve(raw["plan"]["path"]),
            implementer=AgentCfg(
                model=imp["model"],
                permission_mode=_one_of(
                    imp.get("permission_mode", "acceptEdits"),
                    PERMISSION_MODES,
                    "implementer.permission_mode",
                ),
                max_turns=imp.get("max_turns", 80),
                max_budget_usd=imp.get("max_budget_usd"),
            ),
            reviewer=ReviewerCfg(
                model=rev["model"],
                # reviewer 只有唯讀工具,不需要 acceptEdits
                permission_mode=_one_of(
                    rev.get("permission_mode", "default"),
                    PERMISSION_MODES,
                    "reviewer.permission_mode",
                ),
                max_turns=rev.get("max_turns", 40),
                max_budget_usd=rev.get("max_budget_usd"),
                type=_one_of(rev.get("type", "script"), REVIEWER_TYPES, "reviewer.type"),
                poll_interval_sec=rev.get("poll_interval_sec", 60),
                poll_timeout_sec=rev.get("poll_timeout_sec", 1800),
                ocr_exe=rev.get("ocr_exe", "ocr"),
                ocr_timeout_sec=rev.get("ocr_timeout_sec", 300),
                ocr_exclude=rev.get("ocr_exclude", "") or "",
                ocr_rule_path=rev.get("ocr_rule_path", "") or "",
                ocr_max_rule_chars=rev.get("ocr_max_rule_chars", 40000),
            ),
            loop=LoopCfg(
                max_review_rounds=loop.get("max_review_rounds", 5),
                compact_between_rounds=loop.get("compact_between_rounds", True),
                merge_method=_one_of(
                    loop.get("merge_method", "squash"),
                    MERGE_METHODS,
                    "loop.merge_method",
                ),
                merge_gate=_one_of(
                    loop.get("merge_gate", "auto"), MERGE_GATES, "loop.merge_gate",
                ),
                merge_gate_from_milestone=loop.get("merge_gate_from_milestone", 1),
                gate_on_unresolved=loop.get("gate_on_unresolved", True),
                gate_on_agent_error=loop.get("gate_on_agent_error", True),
                # 自由字串 / 整數,不需要 _one_of
                verify_command=loop.get("verify_command", "") or "",
                verify_timeout_sec=loop.get("verify_timeout_sec", 900),
            ),
            notify=NotifyCfg(
                channels=list(channels),
                mention=noti.get("mention", "") or "",
                webhook_url=webhook_url,
                webhook_format=_one_of(
                    noti.get("webhook_format", "discord"),
                    WEBHOOK_FORMATS,
                    "notify.webhook_format",
                ),
            ),
            backend=_one_of(raw.get("backend", "claude"), BACKENDS, "backend"),
            state_file=_resolve(raw.get("state_file", ".pipeline-state.json")),
            config_hint=str(path),
        )

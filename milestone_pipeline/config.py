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
REVIEWER_TYPES = frozenset({"script", "actions"})


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
    type: str = "script"  # "script" | "actions"
    poll_interval_sec: int = 60
    poll_timeout_sec: int = 1800


@dataclass
class LoopCfg:
    max_review_rounds: int = 5
    compact_between_rounds: bool = True
    merge_method: str = "squash"


@dataclass
class Config:
    repo_path: Path
    base_branch: str
    remote: str
    plan_path: Path
    implementer: AgentCfg
    reviewer: ReviewerCfg
    loop: LoopCfg = field(default_factory=LoopCfg)
    state_file: Path = Path(".pipeline-state.json")

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
            ),
            loop=LoopCfg(
                max_review_rounds=loop.get("max_review_rounds", 5),
                compact_between_rounds=loop.get("compact_between_rounds", True),
                merge_method=_one_of(
                    loop.get("merge_method", "squash"),
                    MERGE_METHODS,
                    "loop.merge_method",
                ),
            ),
            state_file=_resolve(raw.get("state_file", ".pipeline-state.json")),
        )

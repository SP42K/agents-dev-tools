"""Reviewer 抽象層:

- ScriptReviewer(純 SDK 版):orchestrator 直接用 opus 起一個 fresh session
  做 review,意見同時留在 PR 上(gh pr review),verdict 回傳給迴圈。
- ActionsReviewer(混合版):review 由 repo 裡的 GitHub Actions workflow
  在 PR 事件觸發時執行,這裡只負責輪詢等結果。

之後要從純 SDK 版切到混合版,改 pipeline.yaml 的 reviewer.type 即可,
implementer 那側完全不用動。
"""
from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from .config import ReviewerCfg
from .gh import Gh
from .prompts import REVIEWER_SYSTEM, review_prompt
from .runner import collect_response


@dataclass
class ReviewResult:
    approved: bool
    feedback: str
    cost_usd: float = 0.0


class Reviewer(ABC):
    @abstractmethod
    async def review(self, pr_number: int, round_no: int,
                     plan_excerpt: str) -> ReviewResult: ...


class ScriptReviewer(Reviewer):
    """每輪都是 fresh context 的 reviewer(不被 implementer 思路污染)。"""

    def __init__(self, cfg: ReviewerCfg, repo_path: Path):
        self.cfg = cfg
        self.repo_path = repo_path

    async def review(self, pr_number: int, round_no: int,
                     plan_excerpt: str) -> ReviewResult:
        options = ClaudeAgentOptions(
            model=self.cfg.model,
            cwd=str(self.repo_path),
            system_prompt=REVIEWER_SYSTEM,
            permission_mode=self.cfg.permission_mode,
            # 讀 + gh 用的 Bash;不給 Edit/Write,reviewer 不改 code
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            max_turns=self.cfg.max_turns,
            max_budget_usd=self.cfg.max_budget_usd,
        )
        result = await collect_response(
            query(prompt=review_prompt(pr_number, round_no, plan_excerpt),
                  options=options)
        )
        approved = self._parse_verdict(result.text)
        return ReviewResult(approved=approved, feedback=result.text,
                            cost_usd=result.cost_usd)

    @staticmethod
    def _parse_verdict(text: str) -> bool:
        m = re.findall(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", text)
        if not m:
            # 解析不到 verdict 時保守處理:視為要求修改,把全文丟給 fixer
            return False
        return m[-1] == "APPROVE"


class ActionsReviewer(Reviewer):
    """混合版:等 GitHub Actions 上的 reviewer workflow 留下新 review。

    前提:repo 已裝好「on pull_request [opened, synchronize] 時跑 opus review」
    的 workflow。這裡記住輪詢起點的 review 數量,等新 review 出現。
    """

    def __init__(self, cfg: ReviewerCfg, repo_path: Path):
        self.cfg = cfg
        self.gh = Gh(repo_path)

    async def review(self, pr_number: int, round_no: int,
                     plan_excerpt: str) -> ReviewResult:
        baseline = len(self.gh.pr_view(pr_number, "reviews").get("reviews") or [])
        deadline = time.monotonic() + self.cfg.poll_timeout_sec

        while time.monotonic() < deadline:
            reviews = self.gh.pr_view(pr_number, "reviews").get("reviews") or []
            if len(reviews) > baseline:
                latest = reviews[-1]
                return ReviewResult(
                    approved=(latest.get("state") == "APPROVED"),
                    feedback=latest.get("body", ""),
                )
            await asyncio.sleep(self.cfg.poll_interval_sec)

        raise TimeoutError(
            f"等不到 PR #{pr_number} 的新 review(第 {round_no} 輪);"
            "檢查 Actions workflow 是否有跑。"
        )


def make_reviewer(cfg: ReviewerCfg, repo_path: Path) -> Reviewer:
    if cfg.type == "actions":
        return ActionsReviewer(cfg, repo_path)
    return ScriptReviewer(cfg, repo_path)

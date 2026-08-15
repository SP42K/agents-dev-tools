"""Reviewer 抽象層:

- ScriptReviewer(純 SDK 版):orchestrator 直接用 opus 起一個 fresh session
  做 review,意見同時留在 PR 上(gh pr review),verdict 回傳給迴圈。
- ActionsReviewer(混合版):review 由 repo 裡的 GitHub Actions workflow
  在 PR 事件觸發時執行,這裡只負責輪詢等結果。
- HybridReviewer(OCR 委託版):先用 `ocr delegate` 確定性地算出該審哪些檔、
  每個檔套哪組檢查項目(不呼叫 LLM),再交給跟 ScriptReviewer 同一套
  Claude session 去讀 diff、跑測試、驗規格、下最終 verdict。

之後要換 reviewer,改 pipeline.yaml 的 reviewer.type 即可,
implementer 那側完全不用動。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from .config import ReviewerCfg
from .gh import Gh
from .ocr import Ocr, OcrError
from .prompts import (
    REVIEWER_SYSTEM,
    format_review_plan,
    hybrid_review_prompt,
    ocr_unavailable_note,
    review_prompt,
)
from .runner import collect_response

log = logging.getLogger(__name__)

# reviewer 不改 code:`tools` 只給唯讀工具 + Bash(跑 gh)。
# 注意 allowed_tools 只是「免詢問」清單,並不會限制工具存在與否,
# 真正的限制要靠 `tools`。
REVIEWER_TOOLS = ["Read", "Glob", "Grep", "Bash"]

# 只認「自成一行」的 VERDICT,避免比對到 prompt 或內文裡的順帶提及。
_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(APPROVE|REQUEST_CHANGES)\s*$", re.MULTILINE)


@dataclass
class ReviewResult:
    approved: bool
    feedback: str
    cost_usd: float = 0.0
    is_error: bool = False


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
        return await self.ask_claude(
            review_prompt(pr_number, round_no, plan_excerpt))

    async def ask_claude(self, prompt: str) -> ReviewResult:
        """起一個 fresh reviewer session 跑 prompt,解析 verdict。

        獨立成方法是給 HybridReviewer 共用的 —— 兩者的 session 設定、
        工具白名單、verdict 解析都必須完全一致,只有 prompt 不同。
        """
        options = ClaudeAgentOptions(
            model=self.cfg.model,
            cwd=str(self.repo_path),
            system_prompt=REVIEWER_SYSTEM,
            permission_mode=self.cfg.permission_mode,
            tools=REVIEWER_TOOLS,
            allowed_tools=REVIEWER_TOOLS,
            max_turns=self.cfg.max_turns,
            max_budget_usd=self.cfg.max_budget_usd,
        )
        result = await collect_response(query(prompt=prompt, options=options))
        approved = self._parse_verdict(result.text)
        return ReviewResult(approved=approved, feedback=result.text,
                            cost_usd=result.cost_usd, is_error=result.is_error)

    @staticmethod
    def _parse_verdict(text: str) -> bool:
        m = _VERDICT_RE.findall(text)
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

    def _review_count(self, pr_number: int) -> int:
        return len(self.gh.pr_view(pr_number, "reviews").get("reviews") or [])

    async def review(self, pr_number: int, round_no: int,
                     plan_excerpt: str) -> ReviewResult:
        # gh 是同步 subprocess,丟到 thread 以免卡住 event loop。
        baseline = await asyncio.to_thread(self._review_count, pr_number)
        deadline = time.monotonic() + self.cfg.poll_timeout_sec

        while time.monotonic() < deadline:
            await asyncio.sleep(self.cfg.poll_interval_sec)
            data = await asyncio.to_thread(self.gh.pr_view, pr_number, "reviews")
            reviews = data.get("reviews") or []
            if len(reviews) > baseline:
                latest = reviews[-1]
                return ReviewResult(
                    approved=(latest.get("state") == "APPROVED"),
                    feedback=latest.get("body", ""),
                )

        raise TimeoutError(
            f"等不到 PR #{pr_number} 的新 review(第 {round_no} 輪);"
            "檢查 Actions workflow 是否有跑。"
        )


class HybridReviewer(Reviewer):
    """open-code-review 委託模式 + Claude reviewer 下 verdict。

    分工:`ocr delegate` 用確定性的工程邏輯算出「該審哪些檔、每個檔該套哪組
    檢查項目」(不呼叫任何 LLM),Claude 拿著這份清單去讀 diff、跑測試、
    驗規格、下 VERDICT。所有判斷仍然只由 Claude 做 —— OCR 連程式碼都沒讀過。

    這樣整條流程只需要一組 Claude 認證(訂閱制也能跑),不會有第二筆帳單。

    OCR 那一段刻意 **fail-open**:沒裝 `ocr`、逾時、JSON 壞掉,都只記警告並把
    失敗原因寫進 prompt,不設 is_error、不 park。理由同
    `prompts.parse_unresolved` —— 下游還有 VERDICT 這道 fail-closed 關卡擋著,
    而 fail-closed 會讓一台沒裝 ocr 的機器每輪 review 都停下來等人。
    """

    def __init__(self, cfg: ReviewerCfg, repo_path: Path, base_branch: str):
        self.cfg = cfg
        self.repo_path = repo_path
        self.base_branch = base_branch
        self.script = ScriptReviewer(cfg, repo_path)
        self.gh = Gh(repo_path)
        self.ocr = Ocr(repo_path, cfg.ocr_exe)

    async def review(self, pr_number: int, round_no: int,
                     plan_excerpt: str) -> ReviewResult:
        section = await asyncio.to_thread(self._scan, pr_number)
        return await self.script.ask_claude(
            hybrid_review_prompt(pr_number, round_no, plan_excerpt, section))

    def _scan(self, pr_number: int) -> str:
        """跑 delegate preview + rule,轉成 prompt 片段。失敗就回退成說明文字。"""
        try:
            head = self.gh.pr_view(pr_number, "headRefName")["headRefName"]
            plan = self.ocr.preview(
                self.base_branch, head,
                exclude=self.cfg.ocr_exclude,
                rule_path=self.cfg.ocr_rule_path or None,
                timeout_sec=self.cfg.ocr_timeout_sec)
            groups = self.ocr.rules(
                plan.paths, self.base_branch, head,
                rule_path=self.cfg.ocr_rule_path or None,
                timeout_sec=self.cfg.ocr_timeout_sec) if plan.paths else []
        except (OcrError, RuntimeError, KeyError, OSError) as e:
            # fail-open:記警告,並讓 reviewer 在 PR 意見裡也看得到這件事,
            # 免得 OCR 長期悄悄沒在跑卻沒人發現。
            log.warning("open-code-review 這輪沒跑成功,退回純 Claude review:%s", e)
            return ocr_unavailable_note(str(e))

        log.info("open-code-review delegate:%d 個檔案待審、%d 個被略過、%d 組規則",
                 len(plan.reviewable), len(plan.excluded), len(groups))
        return format_review_plan(plan, groups, self.cfg.ocr_max_rule_chars)


def make_reviewer(cfg: ReviewerCfg, repo_path: Path,
                  base_branch: str = "main") -> Reviewer:
    if cfg.type == "actions":
        return ActionsReviewer(cfg, repo_path)
    if cfg.type == "hybrid":
        return HybridReviewer(cfg, repo_path, base_branch)
    return ScriptReviewer(cfg, repo_path)

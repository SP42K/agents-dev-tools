"""gh CLI 的薄封裝。orchestrator 自己做「確定性」的 git/GitHub 操作
(查 PR、merge),agent 只負責需要智慧的部分。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


class Gh:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def _run(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            args, cwd=self.repo_path, capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"command failed: {' '.join(args)}\n{proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    # -- git -----------------------------------------------------------------

    def checkout_base(self, base: str, remote: str) -> None:
        self._run("git", "fetch", remote)
        self._run("git", "checkout", base)
        self._run("git", "pull", remote, base)

    def checkout(self, branch: str) -> None:
        """切到指定分支。驗收命令必須跑在 PR 的分支上,而 checkout_base 只在
        PH_IMPLEMENT 開頭跑過 —— crash 後從 PH_REVIEW resume 時 repo 可能
        停在任何分支。"""
        self._run("git", "checkout", branch)

    # -- gh ------------------------------------------------------------------

    def find_pr(self, branch: str) -> int | None:
        out = self._run("gh", "pr", "list", "--head", branch,
                        "--state", "open", "--json", "number", check=False)
        try:
            prs = json.loads(out or "[]")
        except json.JSONDecodeError:
            return None
        return prs[0]["number"] if prs else None

    def pr_view(self, number: int, fields: str) -> dict:
        out = self._run("gh", "pr", "view", str(number), "--json", fields)
        return json.loads(out)

    def latest_review(self, number: int) -> dict | None:
        """回傳最新一筆 review(state: APPROVED / CHANGES_REQUESTED / COMMENTED)。"""
        data = self.pr_view(number, "reviews")
        reviews = data.get("reviews") or []
        return reviews[-1] if reviews else None

    def merge(self, number: int, method: str = "squash") -> None:
        self._run("gh", "pr", "merge", str(number),
                  f"--{method}", "--delete-branch")

    def pr_comment(self, number: int, body: str) -> None:
        self._run("gh", "pr", "comment", str(number), "--body", body)

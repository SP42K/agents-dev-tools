"""`ocr`(alibaba/open-code-review)委託模式的薄封裝。

樣板同 `gh.py`:subprocess 呼叫外部工具,單一職責,不做流程判斷。
「要不要因為 OCR 掛掉而停下 pipeline」這種決策留給 `reviewer.py`。

**只用 `ocr delegate`,不用 `ocr review`。** 兩者的差別是誰出 LLM:
`ocr review` 要自己的 API key(`OCR_LLM_*`),`ocr delegate` 的說明第一行就是
`no LLM required` —— 它只做確定性的工程部分(算 merge-base、挑出該審的檔、
比對出每個檔該套哪組 review 規則),真正的判斷由宿主 agent 用自己的 LLM 做。
在這個 pipeline 裡宿主就是 reviewer 的 Claude session,所以整條流程只需要
一組 Claude 認證,訂閱制也跑得動,而且不會產生第二筆帳單。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 單次 argv 的長度上限。Windows 的命令列硬上限是 32KB,`delegate rule` 是
# 把檔案路徑一個個當參數傳,一個 milestone 動輒 40+ 檔,留一半的餘裕分批。
_ARGV_BUDGET = 16000


@dataclass
class ReviewPlan:
    """`ocr delegate preview --format json` 的結果。"""

    merge_base: str = ""
    reviewable: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    total_insertions: int = 0
    total_deletions: int = 0

    @property
    def paths(self) -> list[str]:
        return [f["path"] for f in self.reviewable if f.get("path")]


@dataclass
class RuleGroup:
    """`ocr delegate rule --format json` 的一組規則(依副檔名 pattern 分組)。"""

    pattern: str = ""
    files: list[str] = field(default_factory=list)
    rule: str = ""


class OcrError(RuntimeError):
    """`ocr` 跑不起來或輸出不可解析。呼叫端決定要 fail-open 還是 fail-closed。"""


class Ocr:
    def __init__(self, repo_path: Path, exe: str = "ocr"):
        self.repo_path = repo_path
        self.exe = exe

    # -- 可獨立測試的純邏輯 ---------------------------------------------------

    def preview_argv(self, base: str, head: str, *, exclude: str = "",
                     rule_path: str | None = None) -> list[str]:
        argv = [self.exe, "delegate", "preview",
                "--from", base, "--to", head, "--format", "json"]
        if exclude:
            # `--exclude` 吃的是**單一個**逗號分隔字串,不是重複傳多次。
            argv += ["--exclude", exclude]
        if rule_path:
            argv += ["--rule", str(rule_path)]
        return argv

    def rule_argv(self, paths: list[str], base: str, head: str, *,
                  rule_path: str | None = None) -> list[str]:
        argv = [self.exe, "delegate", "rule",
                "--from", base, "--to", head, "--format", "json"]
        if rule_path:
            argv += ["--rule", str(rule_path)]
        return argv + list(paths)

    @staticmethod
    def batch_paths(paths: list[str], budget: int = _ARGV_BUDGET) -> list[list[str]]:
        """把檔案路徑切成幾批,免得 argv 撞上 Windows 的命令列長度上限。

        單一路徑就超過預算時仍自成一批 —— 寧可讓 subprocess 自己報錯,
        也不要靜默丟掉檔案。
        """
        batches: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for p in paths:
            cost = len(p) + 1
            if cur and size + cost > budget:
                batches.append(cur)
                cur, size = [], 0
            cur.append(p)
            size += cost
        if cur:
            batches.append(cur)
        return batches

    @staticmethod
    def _load(stdout: str) -> dict:
        text = (stdout or "").strip()
        if not text:
            raise OcrError("ocr 沒有輸出任何內容")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise OcrError(f"ocr 的輸出不是合法 JSON: {e}") from e
        if not isinstance(data, dict):
            raise OcrError("ocr 的 JSON 不是物件,格式可能已變更")
        return data

    @classmethod
    def parse_preview(cls, stdout: str) -> ReviewPlan:
        data = cls._load(stdout)
        if "reviewable_files" not in data:
            raise OcrError("ocr 的 JSON 缺少 `reviewable_files` 欄位,格式可能已變更")
        return ReviewPlan(
            merge_base=str(data.get("merge_base") or ""),
            reviewable=list(data.get("reviewable_files") or []),
            excluded=list(data.get("excluded_files") or []),
            total_insertions=int(data.get("total_insertions") or 0),
            total_deletions=int(data.get("total_deletions") or 0),
        )

    @classmethod
    def parse_rules(cls, stdout: str) -> list[RuleGroup]:
        data = cls._load(stdout)
        if "groups" not in data:
            raise OcrError("ocr 的 JSON 缺少 `groups` 欄位,格式可能已變更")
        return [
            RuleGroup(
                pattern=str(g.get("pattern") or ""),
                files=list(g.get("files") or []),
                rule=str(g.get("rule") or ""),
            )
            for g in (data.get("groups") or [])
        ]

    # -- subprocess -----------------------------------------------------------

    def _resolve_exe(self) -> str:
        """把 `ocr` 解成完整路徑。

        Windows 上 npm 裝出來的是 `ocr.CMD`,而 subprocess 不做 PATHEXT 解析,
        直接傳 "ocr" 會 FileNotFoundError —— 明明 shell 裡跑得動。
        `shutil.which()` 會照 PATHEXT 找,解完的完整路徑才餵得進 CreateProcess。
        """
        return shutil.which(self.exe) or self.exe

    def _run(self, argv: list[str], timeout_sec: int) -> subprocess.CompletedProcess:
        # encoding 一定要指定:Windows 預設 cp950,OCR 的中文輸出不指定就會亂碼。
        return subprocess.run(
            [self._resolve_exe(), *argv[1:]],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )

    def _capture(self, argv: list[str], timeout_sec: int) -> str:
        try:
            proc = self._run(argv, timeout_sec=timeout_sec)
        except FileNotFoundError as e:
            raise OcrError(
                f"找不到 `{self.exe}`,請先 `npm i -g @alibaba-group/open-code-review`"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise OcrError(f"ocr 超過 {timeout_sec}s 未結束") from e
        except OSError as e:
            raise OcrError(f"ocr 啟動失敗: {e}") from e

        if proc.returncode != 0:
            raise OcrError(
                f"ocr 以 exit code {proc.returncode} 結束:"
                f"{(proc.stderr or '').strip()[-800:]}"
            )
        return proc.stdout

    def available(self) -> bool:
        """`ocr` 裝好了嗎。起飛前檢查用,不丟例外。"""
        try:
            proc = self._run([self.exe, "--version"], timeout_sec=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def preview(self, base: str, head: str, *, exclude: str = "",
                rule_path: str | None = None,
                timeout_sec: int = 300) -> ReviewPlan:
        """`ocr delegate preview`:算出這個 diff 該審哪些檔。不呼叫 LLM。"""
        return self.parse_preview(self._capture(
            self.preview_argv(base, head, exclude=exclude, rule_path=rule_path),
            timeout_sec))

    def rules(self, paths: list[str], base: str, head: str, *,
              rule_path: str | None = None,
              timeout_sec: int = 300) -> list[RuleGroup]:
        """`ocr delegate rule`:比對出每個檔該套哪組 review 規則。不呼叫 LLM。

        路徑多時自動分批,再把各批的 group 依 pattern 合併。
        """
        merged: dict[str, RuleGroup] = {}
        for batch in self.batch_paths(paths):
            out = self._capture(
                self.rule_argv(batch, base, head, rule_path=rule_path),
                timeout_sec)
            for g in self.parse_rules(out):
                # 分批會讓同一個 pattern 出現在多批,合併檔案清單,規則取其一。
                if g.pattern in merged:
                    merged[g.pattern].files.extend(g.files)
                else:
                    merged[g.pattern] = g
        return list(merged.values())

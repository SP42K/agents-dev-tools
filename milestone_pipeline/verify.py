"""驗收命令的 subprocess 封裝(reviewer approve 之後的確定性關卡)。

樣板同 `gh.py` / `ocr.py`:一個外部命令、單一職責,不做流程判斷。
「驗收失敗要不要停 pipeline」由 `orchestrator` 決定。

為什麼需要這個:在此之前 milestone 的驗收 100% 靠 reviewer LLM 的 `VERDICT`,
沒有任何確定性檢查 —— `IMPLEMENTER_SYSTEM` 雖然要求「每次修改後跑測試」,
但那是 prompt 自律,orchestrator 從來不驗證。這正好違反 CLAUDE.md 的第一條
原則(「確定性的事一律留在 code 裡,不要交給 prompt 自律」)。
"""
from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VerifyResult:
    ok: bool
    skipped: bool = False       # 沒設 verify_command,等於沒有這道關卡
    output: str = ""            # stdout + stderr,已截斷
    returncode: int | None = None


def _tail(text: str, limit: int) -> str:
    """取尾端(錯誤訊息通常在最後),截斷時一定要註明。

    靜默截斷會讓讀的人以為拿到全文 —— 這裡的讀者是 implementer,
    它會照著殘缺的輸出去猜哪裡壞了。慣例見 `prompts.format_review_plan`。
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"(前面 {len(text) - limit} 字已截斷,以下是輸出的尾端)\n…{text[-limit:]}"


def run_verify(command: str, cwd: Path, timeout_sec: int = 900,
               max_output_chars: int = 4000) -> VerifyResult:
    """跑驗收命令。沒設命令就直接放行(維持沒有這個功能時的行為)。

    `shell=True` 是**刻意**的:使用者要能寫 `pytest && ruff check .`。
    命令來自本機 config 檔、不是 agent 產生的,信任邊界與 `gh.py` / `ocr.py` 相同。
    走 shell 也順便繞開 `ocr.Ocr._resolve_exe()` 那個坑(Windows 上 subprocess
    不做 PATHEXT 解析,`ocr` / `pytest` 這種 .CMD shim 直接傳會 FileNotFoundError)。
    """
    if not command.strip():
        # skipped 時 ok 也是 True,呼叫端只要看 .ok
        return VerifyResult(ok=True, skipped=True)

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            # encoding 一定要指定:Windows 預設 cp950,測試輸出的中文會亂碼。
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        partial = "".join(
            (s.decode("utf-8", "replace") if isinstance(s, bytes) else s or "")
            for s in (e.stdout, e.stderr)
        )
        return VerifyResult(
            ok=False,
            output=_tail(f"驗收命令超過 {timeout_sec}s 未結束,已中止。\n{partial}",
                         max_output_chars),
        )
    except OSError as e:
        return VerifyResult(ok=False, output=f"驗收命令啟動失敗:{e}")

    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return VerifyResult(
        ok=(proc.returncode == 0),
        output=_tail(combined, max_output_chars),
        returncode=proc.returncode,
    )


# -- workspace 指紋:沒變就不重跑失敗的 gate ---------------------------------

def fingerprint(*parts: str) -> str:
    """把幾段文字壓成一個指紋。純函式,方便單獨測試。"""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def workspace_fingerprint(cwd: Path, ignore: Sequence[str] = ()) -> str:
    """HEAD + 未 commit 的變更的指紋。取不到就回空字串。

    呼叫端把空字串當成「無法比對」→ 照跑驗收命令(退化方向安全:寧可多跑
    一次,也不要誤判成「沒動作」而跳過)。

    三個命令缺一不可:

    - `rev-parse HEAD`      → 新 commit
    - `status --porcelain`  → 新增 / 刪除 / 未追蹤的檔案
    - `diff HEAD`           → 已追蹤檔案的**內容**改動

    少了第三個的話,agent 再改一次「本來就已修改」的檔案不會改變狀態碼,
    指紋不變 → 被誤判成沒動作而跳過驗收。

    `ignore` 是要排除的相對路徑,給 pipeline 自己的產物用 —— `.pipeline-state.json`
    每輪都會被 `_save()` 改寫,不排掉的話指紋每次都不一樣,這個快取**永遠不會
    生效**(而且不會有任何症狀,只是白跑)。用 git 的 pathspec magic,
    tracked / untracked 兩種情況都吃得到。
    """
    exclude = [f":(exclude){p}" for p in ignore if p]
    tail = ["--", ".", *exclude] if exclude else []
    parts: list[str] = []
    for argv in (["git", "rev-parse", "HEAD"],
                 ["git", "status", "--porcelain", *tail],
                 ["git", "diff", "HEAD", *tail]):
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if proc.returncode != 0:
            return ""
        parts.append(proc.stdout)
    return fingerprint(*parts)

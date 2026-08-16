"""守護 agent:在 park 點代替人做決策的那個 Claude session。

orchestrator 停下來等人時會 exit,沒有人按 `approve` / `reject` 就永遠停在那裡。
守護 agent 就是那個「人」——它讀 PR、跑驗收、下決策,然後重啟 `run`。

**它唯一不能做的事是自己動手實作。** 這不是靠 prompt 自律,靠的是 `_DENY`:
啟動時用 `--disallowed-tools` 把寫入路徑全部關掉。實測過一次沒關的後果 ——
守護 agent 在 merge gate 上發現一行文件錯誤,判斷「直接修比 reject 省 10 分鐘」,
於是 commit 進分支;那個 commit 沒有經過 reviewer、沒有經過 orchestrator 的
verify、merge 時 CI 還在跑。它讀得懂規矩,只是規矩當時只是文字。

`unsnooze` 是選用的外掛:有裝就用它包起來,撞到用量上限時會在額度重置後
自動喚醒這個 session,守護 agent 醒來再去重啟 pipeline —— 兩層都恢復,
無人值守的跑才接得下去。**不要用 `unsnooze install`**(那會裝全域 hook,
對機器上每個 session 生效);這裡是 per-session 的 launcher 用法,天生只包住
守護 agent 自己。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from . import prompts

log = logging.getLogger("pipeline")

# 守護 agent 的權限邊界。前三項擋掉「產生檔案」,後三項擋掉「讓檔案生效」——
# 兩段都要:`--disallowed-tools Edit Write` 擋不住 Bash 裡的 `cat > file`,
# 而沒有 commit / push,寫出來的東西也進不了 PR。
#
# `gh pr merge` 一併關掉:merge 是 orchestrator 的工作(它要在 merge 後推進
# state),守護 agent 自己 merge 會讓存檔與現實脫節。
_DENY = [
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(gh pr merge:*)",
]


def _resolve(exe: str) -> str | None:
    """把命令解成完整路徑;找不到回 None。

    Windows 上 npm 裝出來的是 `claude.CMD` / `unsnooze.CMD`,而 subprocess
    不做 PATHEXT 解析,直接傳 `"claude"` 會 FileNotFoundError —— 明明 shell
    裡跑得動。同 `ocr.Ocr._resolve_exe()`。
    """
    return shutil.which(exe)


def has_global_unsnooze_hook(settings_path: Path) -> bool:
    """偵測 `unsnooze install` 裝過的全域 hook。

    有的話 unsnooze 會對這台機器上**每個** session 生效,而不是只有守護 agent。
    這不會壞事,但範圍跟這裡的設計不同(見模組 docstring),值得講一聲。
    讀不到 / 不是 JSON 一律當成沒裝 —— 這只是提醒,不該擋住啟動。
    """
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return "unsnooze" in json.dumps(data.get("hooks", {}))


def build_argv(config_path: str, claude_exe: str,
               unsnooze_exe: str | None = None) -> list[str]:
    """組出啟動守護 agent 的完整命令列。

    抽成純函式是為了測得到 —— 這串東西的價值全在「有沒有真的帶上 `_DENY`」,
    而那件事不該只靠人肉核對啟動指令。
    """
    argv = [
        claude_exe,
        "--disallowed-tools", *_DENY,
        "--append-system-prompt", prompts.GUARDIAN_SYSTEM,
        prompts.guardian_task(config_path),
    ]
    # unsnooze 的預設用法就是 `unsnooze [claude args...]`,per-session,
    # 不需要(也不應該)裝全域 hook。
    return [unsnooze_exe, *argv] if unsnooze_exe else argv


def run(config_path: str) -> int:
    """啟動守護 agent,回傳它的 exit code。

    這裡刻意用 `subprocess.call` 而不是捕捉輸出:守護 agent 是互動式 session,
    stdio 要直接接到終端機,unsnooze 也要看得到那個 pane 才醒得過來。

    cwd 用 config 所在的目錄 —— 那就是人跑 `python -m milestone_pipeline` 的地方,
    守護 agent 下的每個指令都要在那裡才對得上(`--config` 是相對路徑時尤其)。
    config 本身在 `__main__` 就載過了,走到這裡代表它是有效的。
    """
    claude_exe = _resolve("claude")
    if not claude_exe:
        raise SystemExit(
            "找不到 `claude` CLI。守護 agent 要用它啟動一個互動式 session,"
            "請先安裝 Claude Code(npm i -g @anthropic-ai/claude-code)。"
        )

    unsnooze_exe = _resolve("unsnooze")
    if unsnooze_exe:
        if has_global_unsnooze_hook(Path.home() / ".claude" / "settings.json"):
            log.warning(
                "偵測到 `unsnooze install` 裝的全域 hook —— unsnooze 會對這台機器上"
                "每個 claude session 生效,不只守護 agent。要只包守護 agent 的話"
                "跑 `unsnooze uninstall`(這裡本來就是 per-session 的用法)。"
            )
        log.info("unsnooze 已就緒:撞到用量上限時會在重置後自動喚醒守護 agent。")
    else:
        # Windows 上沒有 tmux,unsnooze 也沒有官方支援 —— 這是預期中的降級,
        # 不是錯誤。守護 agent 照常跑,只是撞到上限時要人自己回來重開。
        log.warning(
            "沒有 `unsnooze`,撞到用量上限後守護 agent 不會自動恢復,"
            "要人回來重開。(unsnooze 需要 tmux / Zellij,Windows 上跑不起來;"
            "pipeline 本來就固定在同一台跑完,見 config 開頭的說明。)"
        )

    argv = build_argv(config_path, claude_exe, unsnooze_exe)
    log.info("啟動守護 agent(唯讀:已關閉 %s)…", ", ".join(_DENY))
    return subprocess.call(argv, cwd=Path(config_path).resolve().parent)

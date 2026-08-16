"""同一份存檔同時只能有一個 orchestrator 在寫。

兩個 `run` 併跑不會噴錯,只會互相覆蓋 `.pipeline-state.json` —— 後存檔的那個
把前一個的 PR 編號 / session_id / 輪數蓋掉,而兩邊都以為自己是對的。
症狀出現時已經是「implementer 接不回 context」或「同一個 milestone 開了兩個 PR」,
從那裡回推非常貴。

**用 OS 的檔案鎖,不是 pid 檔。** process 被 `kill -9`、斷電、或 crash 時
OS 自己會放掉鎖;pid 檔在同樣的情境下會留下一個殘骸,要人手動判斷「這個 pid
是還活著還是被回收了」—— 而那個判斷跨平台寫起來又長又不可靠。
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path


def lock_path(state_file: Path) -> Path:
    """鎖檔的位置。

    抽成函式是因為 orchestrator 要拿它去算 workspace 指紋的忽略清單:
    state 檔多半就放在目標 repo 裡,鎖檔跟著落在那裡,不排掉的話它會被當成
    「工作區有變動」—— 指紋每次都不一樣,而且 reviewer 與 verify gate 的
    「`git status` 乾淨」也會被它弄髒。
    """
    return state_file.with_name(state_file.name + ".lock")


def _acquire(fh) -> None:
    """拿到獨佔鎖,拿不到就丟 OSError。兩個平台的 stdlib 各有一套。"""
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive(state_file: Path) -> Iterator[None]:
    """圈住整段 `run`。已經有人拿著鎖就 `SystemExit`,不等待。

    刻意不等:orchestrator 是無人值守跑的,排隊等另一個 run 結束沒有意義
    (它結束時多半是 park,狀態已經變了),講清楚「已經有一個在跑」比較有用。
    """
    path = lock_path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    fh.seek(0)
    try:
        _acquire(fh)
    except OSError:
        fh.close()
        raise SystemExit(
            f"已經有另一個 `run` 在使用這份存檔({state_file})。\n"
            "兩個 orchestrator 併跑會互相覆蓋存檔,所以這裡直接停下來。\n"
            "先確認那個是不是你要的(log 尾巴會告訴你它跑到哪),"
            "要換掉的話先把它停掉再重跑。"
        ) from None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    finally:
        try:
            _release(fh)
        finally:
            fh.close()

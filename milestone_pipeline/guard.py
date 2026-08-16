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
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import prompts
from .config import Config
from .state import PH_AWAIT_HUMAN, PH_STUCK, PipelineState

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


"""偵測 bypassPermissions 同意對話框用的字串。

**刻意看畫面,不看 `~/.claude.json` 的 `bypassPermissionsModeAccepted`。**
實測(claude-code 2.1.224):那個旗標是 `true`,對話框照跳 —— 拿設定檔當
「會不會跳對話框」的代理,會在真的卡住時回報沒事,比沒有檢查更糟。
同 CLAUDE.md 裡 `_SCOPE_LOCK` 拿輪數代理 `reviewer_seen` 那顆雷。
"""
_BYPASS_PROMPT = "Bypass Permissions mode"


def _pane_text(tmux_exe: str, name: str) -> str:
    """抓 pane 現在的畫面;抓不到回空字串(退化成「沒看到對話框」)。"""
    try:
        p = subprocess.run([tmux_exe, "capture-pane", "-p", "-t", name],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def _accept_bypass_prompt(tmux_exe: str, name: str, tries: int = 10) -> bool:
    """看到 bypassPermissions 的同意對話框就替它按掉,回傳有沒有按到。

    這是程式**替人接受一個安全聲明**,所以兩個界線要守住:

    - **只在畫面上真的有那個對話框時才送鍵。** 盲送 `2` 會打進別的東西
      (例如 agent 正在等的一般 prompt),那就變成隨機注入。
    - **送完要再看一次確認它消失了**,不能送完就宣告成功 —— 同
      `new-session` 之後回頭確認 session 還在的理由。

    對話框要幾百毫秒才畫出來,所以是輪詢而不是睡一個固定長度。
    輪完都沒看到就當作沒跳過(不擋,呼叫端只是不印那句話)。
    """
    for _ in range(tries):
        if _BYPASS_PROMPT in _pane_text(tmux_exe, name):
            break
        time.sleep(0.5)
    else:
        return False

    # `2` 選 "Yes, I accept";選單有時要一個 Enter 才送出,多送一個是安全的
    # (對話框已經收掉的話,那個 Enter 落在空的輸入框上,等於沒事)。
    subprocess.call([tmux_exe, "send-keys", "-t", name, "2"])
    time.sleep(0.5)
    subprocess.call([tmux_exe, "send-keys", "-t", name, "Enter"])

    for _ in range(tries):
        time.sleep(0.5)
        if _BYPASS_PROMPT not in _pane_text(tmux_exe, name):
            return True
    log.warning(
        "送了同意鍵,但 bypassPermissions 的對話框還在 —— 守護 agent 仍卡著。"
        "去按一次:tmux attach -t %s", name)
    return False


def session_name(config_path: str) -> str:
    """tmux session 的名字,同時也是這個守護 agent 的身分。

    一個名字解三件事:**要不要開第二個**(`tmux has-session`)、
    **怎麼回去看**(`tmux attach`)、以及 **unsnooze 醒來要往哪裡打字**。
    不必另外發明 pid 檔 —— session 不在了就是不在了,沒有殘骸要清。

    名字從 config 檔名推:一台機器上一條 pipeline 一個守護 agent,而 config
    本來就是那條 pipeline 的識別。tmux 不吃 `.` 與 `:`,一律換成 `-`。
    """
    stem = re.sub(r"[^0-9A-Za-z_-]", "-", Path(config_path).stem).strip("-")
    return f"guard-{stem or 'pipeline'}"


def repo_warnings(cwd: Path) -> list[str]:
    """起飛前對 orchestrator 自己這個 repo 的檢查。**只警告,不擋。**

    要防的是「跑到的 orchestrator 不是 git 裡那份」:本機改了 `prompts.py`
    沒 commit,行為就跟你以為的不一樣,而這種差異在 log 裡看不出來。

    **刻意不看「在不在 master 上」** —— 分支名不是「code 乾不乾淨」的代理:
    在 feature branch 上開發這條 pipeline 本來就是正常的(`guard` 這個功能
    自己就是在 feature branch 上寫的)。拿分支當代理是 CLAUDE.md 裡
    `_SCOPE_LOCK` 用輪數代理 `reviewer_seen` 那顆雷的同一個形狀。
    """
    def git(*args: str) -> str | None:
        try:
            p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return p.stdout.strip() if p.returncode == 0 else None

    out: list[str] = []
    dirty = git("status", "--porcelain")
    if dirty:
        n = len(dirty.splitlines())
        out.append(
            f"{cwd} 有 {n} 個未 commit 的變動 —— 守護 agent 跑的 orchestrator "
            f"不是 git 裡那份,行為可能跟你以為的不一樣。")
    # 不 fetch:那是網路動作,起飛路徑上不該卡在它。所以這個數字反映的是
    # **上次 fetch 之後**的落後量,抓得到「昨天 pull 完就沒再理它」這種。
    behind = git("rev-list", "--count", "HEAD..@{u}")
    if behind and behind != "0":
        out.append(
            f"{cwd} 落後 upstream {behind} 個 commit(以上次 fetch 為準)——"
            "可能少了已經修好的東西,先 `git pull` 比較保險。")
    return out


def build_argv(config_path: str, claude_exe: str,
               unsnooze_exe: str | None = None) -> list[str]:
    """組出啟動守護 agent 的完整命令列。

    抽成純函式是為了測得到 —— 這串東西的價值全在「有沒有真的帶上 `_DENY`」,
    而那件事不該只靠人肉核對啟動指令。
    """
    argv = [
        claude_exe,
        # 守護 agent 沒有人在旁邊按「允許」。預設的 ask 模式下,第一個不在
        # allowlist 裡的命令(`gh pr view`、`npm test`…)就會停在權限對話框上,
        # 而它是為了無人值守而存在的 —— 那個對話框在這裡不是安全網,是掛住。
        # 實測(formosa M12):agent 醒來開始驗 merge gate,第一個 `gh pr view`
        # 就卡住,pipeline 空轉。
        # 邊界仍然是 code 而不是 prompt:deny 規則的優先權高於權限模式,
        # `_DENY` 照舊生效。放寬的只有「問不問」,不是「能不能」。
        "--permission-mode", "bypassPermissions",
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

    cwd = Path(config_path).resolve().parent
    for w in repo_warnings(cwd):
        log.warning(w)

    argv = build_argv(config_path, claude_exe, unsnooze_exe)
    tmux_exe = _resolve("tmux")
    if not tmux_exe:
        # Windows 走這條。前景跑仍然有 `_DENY` 與系統提示,只是關掉終端就沒了,
        # 而且 unsnooze 沒有 pane 可以喚醒 —— 這是降級,不是錯誤。
        log.warning(
            "沒有 `tmux`,改用前景跑:關掉這個終端守護 agent 就沒了。"
            "(unsnooze 是靠往 pane 裡打字來喚醒的,沒有 tmux 就沒有自動恢復。)"
        )
        log.info("啟動守護 agent(唯讀:已關閉 %s)…", ", ".join(_DENY))
        return subprocess.call(argv, cwd=cwd)

    name = session_name(config_path)
    if _has_session(tmux_exe, name):
        # 開第二個不會噴錯,只會有兩個守護 agent 對同一個 gate 各自下決策 ——
        # 一個 approve 一個 reject,而且兩個都會去重啟 `run`。
        log.warning(
            "已經有一個守護 agent 在顧 `%s`(tmux session `%s`),不再開第二個。\n"
            "  看它:tmux attach -t %s   (離開:ctrl+b d)\n"
            "  換掉:tmux kill-session -t %s 之後再跑一次",
            config_path, name, name, name)
        return 0

    log.info("啟動守護 agent(唯讀:已關閉 %s)…", ", ".join(_DENY))
    # shlex.join:prompt 裡有換行與引號,交給 tmux 自己拆會拆錯。
    rc = subprocess.call(
        [tmux_exe, "new-session", "-d", "-s", name, shlex.join(argv)], cwd=cwd)
    if rc != 0:
        return rc
    # `new-session` 的 rc 只代表 tmux 建得起來,不代表裡面那個命令活著 ——
    # 命令一啟動就死的話 tmux 會馬上收掉 session,而我們會回報「已跑起來」。
    # 立刻回頭確認一次(只抓得到「秒死」,但那正是設定錯時的樣子)。
    if not _has_session(tmux_exe, name):
        log.error("tmux session `%s` 建好之後立刻就沒了 —— 守護 agent 沒能啟動。"
                  "拿掉 tmux 在前景跑一次就看得到原因(例如 claude 需要先登入)。",
                  name)
        return 1
    if _accept_bypass_prompt(tmux_exe, name):
        log.info("已替守護 agent 按掉 bypassPermissions 的同意對話框 —— "
                 "它是 detached 的,沒人會看到它在等。")
    log.info("守護 agent 已在背景的 tmux session `%s` 裡跑起來。\n"
             "  看它:tmux attach -t %s   (離開:ctrl+b d,不會中斷它)\n"
             "  停它:tmux kill-session -t %s", name, name, name)
    return 0


def _has_session(tmux_exe: str, name: str) -> bool:
    return subprocess.call([tmux_exe, "has-session", "-t", name],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


# -- `guards`:一眼看完這台機器上所有守護 agent -------------------------------
#
# 守護 agent 多半跑在別台(`ssh mac`),而回去看它的唯一入口是 `tmux attach -t
# guard-<config stem>` —— 要先記得 session 叫什麼,而且一次只看得到一個。
# 同時開多個專案時,沒有任何地方回答「總共幾條在跑、各自停在哪」。
#
# **這整段是唯讀報表,不是關卡。** 不取 `lock.exclusive()`、不寫 state。

_CLI = "python -m milestone_pipeline"
_NO_GUARD = "(無 guard)"


@dataclass
class GuardRow:
    """一條 pipeline 現在的狀態。**帶資料,不是排好的字串。**

    三個消費者各自排版:`guards` 排文字表、`tgbot` 排 HTML + 按鈕、
    `tgbot` 的 watchdog 只讀 `age_sec` 判斷卡多久。把字串先排好再讓 tgbot
    回頭解析等於自己發明一套格式再自己解 —— 資料留著,排版各自做。
    """
    config: Path | None                # None = 對不到 config 的孤兒 session
    session: str                       # tmux session 名字(不管活著沒)
    repo: Path | None = None           # 目標 repo,tgbot 拿它去問 PR 網址
    alive: bool = False
    started: bool = False              # 有存檔 = 跑過
    milestone: int | None = None
    phase: str = ""
    review_round: int = 0
    cost_usd: float = 0.0
    pr_number: int | None = None
    await_reason: str | None = None
    age_sec: float | None = None       # 存檔多久沒動;None = 沒有存檔
    problem: str = ""                  # config 載不起來 / 孤兒 session 的說明
    escalate_after_min: int = 20       # 停這麼久還沒動就升級推播(tgbot watchdog)

    @property
    def project(self) -> str:
        """指令與 callback 用的識別:config 檔名去掉副檔名。"""
        return self.config.stem if self.config else ""

    @property
    def parked(self) -> bool:
        return self.phase in (PH_STUCK, PH_AWAIT_HUMAN)

    @property
    def attention(self) -> bool:
        """需要人看一眼 → `guards` 的 exit code 1。"""
        return self.parked or bool(self.problem)

    @property
    def age(self) -> str:
        return _ago(self.age_sec) if self.age_sec is not None else "—"

    @property
    def resume_verb(self) -> str:
        """從這個 park 點恢復要用哪個指令。

        `stuck` 是輪數用盡,`approve` 吃不下(它只認 `await_human`)—— 出口是
        `retry`。搞錯的話人會在手機上按到一個一定失敗的按鈕。
        """
        return "approve" if self.phase == PH_AWAIT_HUMAN else "retry"

    def summary(self) -> str:
        """一行摘要(不含 session / 檔名那兩欄,tgbot 也用這個)。"""
        if self.problem:
            return f"⚠ {self.problem}"
        if not self.started:
            return "尚未開跑"
        bits = [f"M{self.milestone} {self.phase}"]
        if self.await_reason:
            bits.append(f"({self.await_reason})")
        if self.review_round:
            bits.append(f"round={self.review_round}")
        if self.cost_usd:
            bits.append(f"~${self.cost_usd:.2f}")
        bits.append(self.age)
        if self.parked:
            bits.append("⚠" if self.alive else "⚠ 沒有守護 agent 在顧")
        return "  ".join(bits)

    def lines(self) -> list[str]:
        """`guards` 的文字輸出(終端機是等寬字,所以這裡才對得起欄位)。"""
        who = self.session if self.alive else _NO_GUARD
        name = self.config.name if self.config else ""
        out = [f"{who:<16}{name:<18}{self.summary()}"]
        if not self.parked or not self.config:
            return out
        # 停下來的話把指令一起印出來,不用去翻 `status`(同 `status` 的作法)。
        tail = f"--config {self.config.name}"
        label = "放行" if self.resume_verb == "approve" else "續跑"
        out.append(f"    {label}: {_CLI} {self.resume_verb} "
                   f"--milestone {self.milestone} {tail}")
        out.append(f"    打回: {_CLI} reject --milestone {self.milestone} "
                   f'--reason "..." {tail}')
        if self.alive:
            out.append(f"    看它: tmux attach -r -t {self.session}")
        return out


def list_sessions(tmux_exe: str) -> list[str]:
    """tmux 裡現在有哪些 session。沒有 server 在跑時 tmux 回 1,一律回 `[]`。"""
    try:
        p = subprocess.run([tmux_exe, "ls", "-F", "#{session_name}"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "剛剛"
    if seconds < 5400:
        return f"{round(seconds / 60)} 分鐘前"
    if seconds < 172800:
        return f"{round(seconds / 3600)} 小時前"
    return f"{round(seconds / 86400)} 天前"


def summarize(config_path: Path, alive: bool, state: PipelineState | None,
              mtime: float | None, now: float,
              repo: Path | None = None) -> GuardRow:
    """把一條 pipeline 的存檔整理成一列。**純函式,不碰檔案系統**(才測得到)。

    ⚠ 只給「停在 `stuck` / `await_human`」。**刻意不設「多久沒動就算卡住」的
    門檻** —— implement 階段跑一小時不寫 state 是正常的,任何時間門檻都會誤報;
    時間照印但不拿來判斷,留給人看。同 `repo_warnings` 不拿分支名當「code 乾不
    乾淨」的代理。(真正的「卡住」由 `tgbot` 的 watchdog 用時間判斷 —— 那裡的
    語意不同:它看的是**停下來之後**過了多久,不是「有沒有在寫存檔」。)

    同理**「沒有 guard session」本身不算問題** —— `nohup … run` 不配守護 agent
    是既有的正常跑法(見 formosa.yaml 檔頭)。只有「停下來等人**而且**沒有守護
    agent 會去按」才值得警告,那才是真的沒人會來。
    """
    row = GuardRow(
        config=config_path,
        session=session_name(str(config_path)),
        repo=repo,
        alive=alive,
        # `is not None`:mtime 是 epoch 秒數,`if mtime` 會把 0.0 當成「沒有」
        age_sec=(now - mtime) if mtime is not None else None,
    )
    if state is None:
        return row

    ms = state.milestones.get(str(state.current))
    row.started = True
    row.milestone = state.current
    row.phase = ms.phase if ms else "pending"
    if ms:
        row.review_round = ms.review_round
        row.cost_usd = ms.cost_usd
        row.pr_number = ms.pr_number
        row.await_reason = ms.await_reason
    return row


def collect(workdir: Path, now: float | None = None) -> list[GuardRow]:
    """掃 `workdir` 底下每個 config,配上 tmux 裡活著的 guard session。

    config 是唯一的識別(`session_name()` 就是從它推出來的),所以這裡不需要、
    也不該另外發明一份「有哪些專案」的登錄檔 —— 目錄裡的 yaml 就是答案。
    """
    now = time.time() if now is None else now
    tmux_exe = _resolve("tmux")
    sessions = set(list_sessions(tmux_exe)) if tmux_exe else set()

    rows: list[GuardRow] = []
    claimed: set[str] = set()
    for p in sorted([*workdir.glob("*.yaml"), *workdir.glob("*.yml")]):
        name = session_name(str(p))
        alive = name in sessions
        claimed.add(name)
        try:
            cfg = Config.load(p)
        except (Exception, SystemExit) as e:
            # `Config.load` 缺欄位丟 `KeyError`、`repo.path` 不存在丟 `SystemExit`
            # —— **`SystemExit` 不是 `Exception` 的子類,兩個都要接**。
            # 目錄裡本來就有不是 pipeline config 的 yaml(還有 `pipeline.yaml`
            # 那份 repo.path 是佔位字串的範本),所以沒有 session 對到就安靜略過;
            # 有 session 對到卻載不起來才是真的壞了,那要看得見。
            if alive:
                rows.append(GuardRow(p, name, alive=True,
                                     problem=f"config 載入失敗: {e}"))
            continue

        sf = cfg.state_file
        try:
            exists = sf.exists()
            state = PipelineState.load(sf) if exists else None
            mtime = sf.stat().st_mtime if exists else None
        except (OSError, ValueError) as e:
            log.warning("讀不到 %s 的存檔 %s:%s", p.name, sf, e)
            state, mtime = None, None
        row = summarize(p, alive, state, mtime, now, repo=cfg.repo_path)
        row.escalate_after_min = cfg.notify.escalate_after_min
        rows.append(row)

    # 對不到 config 的 guard session:config 改名或刪掉了,而那個守護 agent 還在跑。
    for name in sorted(s for s in sessions - claimed if s.startswith("guard-")):
        rows.append(GuardRow(None, name, alive=True,
                             problem="找不到對應的 config(改名或刪掉了?)"))
    return rows

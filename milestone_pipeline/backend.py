"""Agent backend 抽象層:唯一知道 `claude_agent_sdk` 長什麼樣的模組。

`runner.AgentResult`(text / session_id / cost_usd / is_error / subtype)本來就是
天然的介面邊界,這裡只是把它兌現:兩個能力 —— 持久 session(對應 `Implementer`,
一個 milestone 一個 context window)與一次性 query(對應 `ScriptReviewer`,
每輪 fresh session)—— 回傳一律是 `AgentResult`。

**只做介面,不順手實作第二個 backend。** 評估過的替代品(prime-agent)沒有工具
白名單、沒有權限模式、沒有 `max_budget_usd` 的原生對應,換過去會讓
`REVIEWER_TOOLS` 的唯讀**保證**退化成 prompt 自律。理由見
`docs/plans/2026-08-15-external-tool-adoption.md` 的「明確不做」。

附帶收益:注入一個假 backend 就能測 agent 封裝的邏輯,而且不違反「不 mock SDK」
的測試原則 —— 假的是**自家介面**,不是 SDK 型別。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, query

from .config import AgentCfg
from .prompts import COMPACT
from .runner import AgentResult, collect_response

OnText = Callable[[str], None]


@dataclass
class AgentSpec:
    """起一個 agent 需要的全部設定。跨 backend 共通,不含任何 SDK 型別。"""

    cfg: AgentCfg
    repo_path: Path
    system_prompt: str
    tools: list[str]


class AgentSession(ABC):
    """持久 session:多輪對話共用同一個 context window,可 resume。"""

    session_id: str | None = None

    @abstractmethod
    async def __aenter__(self) -> "AgentSession": ...

    @abstractmethod
    async def __aexit__(self, *exc) -> None: ...

    @abstractmethod
    async def ask(self, prompt: str,
                  on_text: OnText | None = None) -> AgentResult: ...

    @abstractmethod
    async def compact(self) -> None:
        """壓縮上下文。做不到的 backend 可以是 no-op。"""


class AgentBackend(ABC):
    @abstractmethod
    def session(self, spec: AgentSpec,
                resume_session_id: str | None = None) -> AgentSession: ...

    @abstractmethod
    async def query_once(self, spec: AgentSpec, prompt: str,
                         on_text: OnText | None = None) -> AgentResult: ...


# -- Claude(claude-agent-sdk)------------------------------------------------

def _options(spec: AgentSpec,
             resume_session_id: str | None = None) -> ClaudeAgentOptions:
    # `tools` 才是工具白名單,`allowed_tools` 只是「這些免詢問」——
    # 要真的拿掉 reviewer 的 Edit/Write 必須設 `tools`。兩個餵同一份清單。
    return ClaudeAgentOptions(
        model=spec.cfg.model,
        cwd=str(spec.repo_path),
        system_prompt=spec.system_prompt,
        permission_mode=spec.cfg.permission_mode,
        tools=spec.tools,
        allowed_tools=spec.tools,
        max_turns=spec.cfg.max_turns,
        max_budget_usd=spec.cfg.max_budget_usd,
        resume=resume_session_id,
    )


class ClaudeSession(AgentSession):
    def __init__(self, spec: AgentSpec, resume_session_id: str | None = None):
        self.spec = spec
        self.session_id = resume_session_id
        self._resume = resume_session_id
        self._client: ClaudeSDKClient | None = None

    async def __aenter__(self) -> "ClaudeSession":
        self._client = ClaudeSDKClient(options=_options(self.spec, self._resume))
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def ask(self, prompt: str,
                  on_text: OnText | None = None) -> AgentResult:
        assert self._client is not None, "use `async with ...`"
        await self._client.query(prompt)
        result = await collect_response(self._client.receive_response(),
                                        on_text=on_text)
        if result.session_id:
            self.session_id = result.session_id
        return result

    async def compact(self) -> None:
        # `/compact` 是 Claude CLI 的斜線指令,所以這件事屬於 backend。
        assert self._client is not None
        await self._client.query(COMPACT)
        await collect_response(self._client.receive_response())


class ClaudeBackend(AgentBackend):
    def session(self, spec: AgentSpec,
                resume_session_id: str | None = None) -> ClaudeSession:
        return ClaudeSession(spec, resume_session_id)

    async def query_once(self, spec: AgentSpec, prompt: str,
                         on_text: OnText | None = None) -> AgentResult:
        return await collect_response(
            query(prompt=prompt, options=_options(spec)), on_text=on_text)


def make_backend(name: str = "claude") -> AgentBackend:
    """樣板同 `reviewer.make_reviewer`。合法值列在 `config.BACKENDS`。

    這裡刻意**不**像 `make_reviewer` 那樣讓不認得的值落到預設:
    `config.BACKENDS` 與這個 dispatch 是兩個必須同步改的地方,只加前者
    (加了新 backend 卻忘了接)會讓 pipeline 靜默跑在錯的 runtime 上 ——
    而 backend 決定的是工具白名單與預算閘門,錯了不會有症狀,只會失去保證。
    """
    if name != "claude":
        raise SystemExit(
            f"backend {name!r} 在 config.BACKENDS 裡但 make_backend() 還沒接;"
            "兩個地方要一起改。")
    return ClaudeBackend()

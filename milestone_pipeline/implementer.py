"""Implementer / Fixer:一個 milestone 一個持久 session。

實作與後續每一輪修復都跑在同一個 context window 裡,
所以 fixer 記得自己當初的設計決策;輪與輪之間可下 /compact 壓縮上下文。
crash 後可用存下來的 session_id resume。

SDK 的呼叫細節在 `backend.py`,這裡只負責「implementer 是什麼」:
system prompt、工具白名單、以及要不要把文字即時吐進 log。
"""
from __future__ import annotations

from pathlib import Path

from .backend import AgentBackend, AgentSpec
from .config import AgentCfg
from .prompts import IMPLEMENTER_SYSTEM
from .runner import AgentResult, log_text

# implementer 要能改 code、跑測試、用 git/gh,所以需要寫入類工具。
# `tools` 決定「有哪些工具存在」,`allowed_tools` 決定「哪些不用問就能用」
# (兩者都由 backend 餵給 SDK)。
IMPLEMENTER_TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]


class Implementer:
    def __init__(self, cfg: AgentCfg, repo_path: Path, backend: AgentBackend,
                 resume_session_id: str | None = None):
        spec = AgentSpec(cfg=cfg, repo_path=repo_path,
                         system_prompt=IMPLEMENTER_SYSTEM,
                         tools=IMPLEMENTER_TOOLS)
        self._session = backend.session(spec, resume_session_id)
        self._echo = log_text("implementer")

    @property
    def session_id(self) -> str | None:
        return self._session.session_id

    async def __aenter__(self) -> "Implementer":
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._session.__aexit__(*exc)

    async def ask(self, prompt: str) -> AgentResult:
        return await self._session.ask(prompt, on_text=self._echo)

    async def compact(self) -> None:
        """輪間壓縮:保留設計決策與未解決事項,丟掉實作細節的冗長過程。

        刻意**不**接 on_text —— 壓縮過程對看 log 的人是雜訊。
        """
        await self._session.compact()

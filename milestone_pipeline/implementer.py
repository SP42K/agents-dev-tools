"""Implementer / Fixer:一個 milestone 一個持久 session。

實作與後續每一輪修復都跑在同一個 context window 裡,
所以 fixer 記得自己當初的設計決策;輪與輪之間可下 /compact 壓縮上下文。
crash 後可用存下來的 session_id resume。
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from .config import AgentCfg
from .prompts import COMPACT, IMPLEMENTER_SYSTEM
from .runner import AgentResult, collect_response


class Implementer:
    def __init__(self, cfg: AgentCfg, repo_path: Path,
                 resume_session_id: str | None = None):
        self.cfg = cfg
        self.repo_path = repo_path
        self.resume_session_id = resume_session_id
        self.session_id: str | None = resume_session_id
        self._client: ClaudeSDKClient | None = None

    def _options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self.cfg.model,
            cwd=str(self.repo_path),
            system_prompt=IMPLEMENTER_SYSTEM,
            permission_mode=self.cfg.permission_mode,
            allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
            max_turns=self.cfg.max_turns,
            max_budget_usd=self.cfg.max_budget_usd,
            resume=self.resume_session_id,
        )

    async def __aenter__(self) -> "Implementer":
        self._client = ClaudeSDKClient(options=self._options())
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def ask(self, prompt: str) -> AgentResult:
        assert self._client is not None, "use `async with Implementer(...)`"
        await self._client.query(prompt)
        result = await collect_response(self._client.receive_response())
        if result.session_id:
            self.session_id = result.session_id
        return result

    async def compact(self) -> None:
        """輪間壓縮:保留設計決策與未解決事項,丟掉實作細節的冗長過程。"""
        assert self._client is not None
        await self._client.query(COMPACT)
        await collect_response(self._client.receive_response())

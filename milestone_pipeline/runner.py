"""SDK 共用小工具:收集一次 response 的文字、session_id 與花費。"""
from __future__ import annotations

from dataclasses import dataclass

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


@dataclass
class AgentResult:
    text: str                       # assistant 的文字輸出(含最終回報)
    session_id: str | None = None
    cost_usd: float = 0.0           # 該 session 目前累計花費
    is_error: bool = False


async def collect_response(message_iter) -> AgentResult:
    """吃完一次 receive_response(),整理成 AgentResult。"""
    chunks: list[str] = []
    session_id = None
    cost = 0.0
    is_error = False

    async for msg in message_iter:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(msg, ResultMessage):
            session_id = getattr(msg, "session_id", None)
            cost = getattr(msg, "total_cost_usd", None) or 0.0
            subtype = getattr(msg, "subtype", "")
            is_error = bool(subtype) and subtype != "success"
            result = getattr(msg, "result", None)
            if isinstance(result, str) and result.strip():
                chunks.append(result)

    return AgentResult(
        text="\n".join(chunks).strip(),
        session_id=session_id,
        cost_usd=cost,
        is_error=is_error,
    )

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
    subtype: str = ""               # "success" / "error_max_turns" / "error_max_budget_usd" …


async def collect_response(message_iter) -> AgentResult:
    """吃完一次 receive_response(),整理成 AgentResult。"""
    chunks: list[str] = []
    session_id = None
    cost = 0.0
    is_error = False
    subtype = ""
    final = ""

    async for msg in message_iter:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(msg, ResultMessage):
            session_id = getattr(msg, "session_id", None)
            cost = getattr(msg, "total_cost_usd", None) or 0.0
            subtype = getattr(msg, "subtype", "") or ""
            # ResultMessage.is_error 也會在 API 層失敗(429/5xx)時為 True,
            # 那時 subtype 仍是 "success",所以兩者都要看。
            is_error = bool(getattr(msg, "is_error", False)) or (
                bool(subtype) and subtype != "success"
            )
            result = getattr(msg, "result", None)
            if isinstance(result, str):
                final = result.strip()

    text = "\n".join(chunks).strip()
    # `ResultMessage.result` 正常情況下就是最後一則 assistant 文字的複本,
    # 無條件接上去會讓整段回覆重複一次 —— reviewer 的意見會原封不動被
    # 餵給 fixer 兩份,而且每輪 fix_prompt 都再嵌一次,愈滾愈大。
    # 只在它真的帶來新東西時才接(例如 max_turns 中斷時 chunks 是空的,
    # 或 SDK 把錯誤說明放在 result 裡)。
    if final and final not in text:
        text = f"{text}\n{final}".strip() if text else final

    return AgentResult(
        text=text,
        session_id=session_id,
        cost_usd=cost,
        is_error=is_error,
        subtype=subtype,
    )

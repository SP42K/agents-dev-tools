"""SDK 共用小工具:收集一次 response 的文字、session_id 與花費。"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


@dataclass
class AgentResult:
    text: str                       # assistant 的文字輸出(含最終回報)
    session_id: str | None = None
    cost_usd: float = 0.0           # 該 session 目前累計花費
    is_error: bool = False
    subtype: str = ""               # "success" / "error_max_turns" / "error_max_budget_usd" …


def log_text(role: str) -> Callable[[str], None]:
    """做出一個把 agent 文字即時寫進 log 的 callable,給 collect_response 的 on_text。

    agent 一輪要跑 20-40 分鐘,整段收完才回傳的話這期間 log 完全空白,
    人只能去看目標 repo 的 git 痕跡猜它在幹嘛。

    刻意**不**寫 stdout —— 無人值守跑時 handler 由 `__main__` 決定。
    一個 TextBlock 一則 record(內容多行就多行),不逐行拆:拆行只換來 grep
    方便,卻讓每則訊息長出一堆 timestamp。
    """
    rlog = logging.getLogger(f"pipeline.{role}")

    def _emit(text: str) -> None:
        body = text.strip()
        if body:
            rlog.info("[%s] %s", role, body)

    return _emit


async def collect_response(
    message_iter, on_text: Callable[[str], None] | None = None
) -> AgentResult:
    """吃完一次 receive_response(),整理成 AgentResult。

    `on_text` 是可選的旁路:每收到一塊 assistant 文字就呼叫一次,讓呼叫端
    可以即時輸出。它**不影響**回傳值 —— `AgentResult` 的語意完全不變。
    """
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
                    if on_text is not None:
                        on_text(block.text)
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

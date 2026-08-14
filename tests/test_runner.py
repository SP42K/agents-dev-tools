"""collect_response 的純邏輯測試。

不 mock SDK client,只餵假的 message 序列 —— 這裡要測的是「怎麼把訊息串
整理成 AgentResult」,那是純函式邏輯。
"""
from __future__ import annotations

import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from milestone_pipeline.runner import collect_response


def _assistant(*texts: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=t) for t in texts],
        model="test",
    )


def _result(text: str | None, *, subtype: str = "success",
            is_error: bool = False, cost: float = 0.0) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=0,
        duration_api_ms=0,
        is_error=is_error,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=cost,
        result=text,
    )


def _collect(messages) -> object:
    async def gen():
        for m in messages:
            yield m

    return asyncio.run(collect_response(gen()))


def test_result_duplicate_is_not_appended() -> None:
    """`ResultMessage.result` 多半是最後一則 assistant 文字的複本,不該重複收錄。"""
    body = "review 意見全文\n\nVERDICT: APPROVE"
    out = _collect([_assistant(body), _result(body)])
    assert out.text == body
    assert out.text.count("VERDICT: APPROVE") == 1


def test_result_used_when_no_assistant_text() -> None:
    """中斷時 assistant 可能一則文字都沒有,這時 result 是唯一的線索。"""
    out = _collect([_result("max turns reached", subtype="error_max_turns",
                            is_error=True)])
    assert out.text == "max turns reached"
    assert out.is_error is True
    assert out.subtype == "error_max_turns"


def test_result_appended_when_it_adds_something() -> None:
    out = _collect([_assistant("做到一半"), _result("預算用完了")])
    assert out.text == "做到一半\n預算用完了"


def test_multiple_assistant_blocks_joined() -> None:
    out = _collect([_assistant("第一段", "第二段"), _assistant("第三段"),
                    _result("第三段")])
    assert out.text == "第一段\n第二段\n第三段"


def test_api_failure_flagged_even_when_subtype_is_success() -> None:
    """API 層失敗時 is_error=True 但 subtype 仍是 success,只看一個會漏。"""
    out = _collect([_assistant("x"), _result("x", is_error=True)])
    assert out.is_error is True


def test_session_id_and_cost_taken_from_result() -> None:
    out = _collect([_assistant("x"), _result("x", cost=1.25)])
    assert out.session_id == "sess-1"
    assert out.cost_usd == 1.25

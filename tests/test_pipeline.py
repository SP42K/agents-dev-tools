"""不需要 SDK / gh / 網路的純邏輯測試。"""
from __future__ import annotations

import json

import pytest
import yaml

from milestone_pipeline.config import Config, LoopCfg
from milestone_pipeline.plan import Plan
from milestone_pipeline.state import (PH_AWAIT_HUMAN, PH_MERGE, MilestoneState,
                                      PipelineState)


# -- plan 解析 ---------------------------------------------------------------

def _plan(tmp_path, text):
    p = tmp_path / "plan.md"
    p.write_text(text, encoding="utf-8")
    return Plan.load(p)


def test_plan_splits_milestones_and_strips_prefix(tmp_path):
    plan = _plan(tmp_path, "intro\n\n## Milestone 1: Data model\nbody a\n\n"
                           "## Milestone 2:API\nbody b\n")
    assert plan.preamble == "intro"
    assert [m.title for m in plan.milestones] == ["Data model", "API"]
    assert plan.milestones[0].body == "body a"


def test_plan_ignores_h3_and_empty_trailing_heading(tmp_path):
    # 舊版會在這裡 IndexError
    plan = _plan(tmp_path, "intro\n\n## Real\nbody\n### Sub\nmore\n\n## ")
    assert len(plan.milestones) == 1
    assert "### Sub" in plan.milestones[0].body


def test_plan_indexes_are_contiguous_after_skipping(tmp_path):
    plan = _plan(tmp_path, "intro\n\n## \n\n## A\nx\n\n## B\ny\n")
    assert [m.index for m in plan.milestones] == [1, 2]


def test_plan_requires_at_least_one_milestone(tmp_path):
    with pytest.raises(SystemExit):
        _plan(tmp_path, "just a preamble, no headings\n")


def test_slug_keeps_cjk_and_falls_back(tmp_path):
    plan = _plan(tmp_path, "x\n\n## 建立資料模型\nb\n\n## !!!\nb\n")
    assert plan.milestones[0].slug == "建立資料模型"
    assert plan.milestones[0].branch == "milestone/1-建立資料模型"
    # 標題完全沒有可用字元時退回 milestone-N
    assert plan.milestones[1].slug == "milestone-2"


def test_slug_normalises_spaces_and_punctuation(tmp_path):
    plan = _plan(tmp_path, "x\n\n## Add API endpoints (v2)!\nb\n")
    assert plan.milestones[0].slug == "add-api-endpoints-v2"


# -- verdict 解析 ------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("review text\nVERDICT: APPROVE", True),
    ("review text\nVERDICT: REQUEST_CHANGES", False),
    ("no verdict at all", False),                       # 保守:視為要求修改
    ("VERDICT: APPROVE\nVERDICT: REQUEST_CHANGES", False),  # 取最後一個
    ("VERDICT: REQUEST_CHANGES\nVERDICT: APPROVE", True),
    ("  VERDICT:APPROVE  ", True),                      # 容忍空白
])
def test_parse_verdict(text, expected):
    from milestone_pipeline.reviewer import ScriptReviewer
    assert ScriptReviewer._parse_verdict(text) is expected


def test_parse_verdict_ignores_inline_mention():
    """行內順帶提到的 VERDICT 不該蓋掉真正的結論。"""
    from milestone_pipeline.reviewer import ScriptReviewer
    text = ("我等一下會輸出 VERDICT: REQUEST_CHANGES 或 VERDICT: APPROVE。\n"
            "看起來沒問題。\n"
            "VERDICT: APPROVE")
    assert ScriptReviewer._parse_verdict(text) is True


# -- state 持久化 ------------------------------------------------------------

def test_state_roundtrip_and_cost_total(tmp_path):
    st = PipelineState()
    ms = st.ms(1)
    ms.phase = PH_MERGE
    ms.pr_number = 7
    ms.implementer_cost_usd = 2.5
    ms.reviewer_cost_usd = 1.25
    st.save(tmp_path / "s.json")

    back = PipelineState.load(tmp_path / "s.json")
    assert back.ms(1).pr_number == 7
    assert back.ms(1).cost_usd == 3.75


def test_state_load_drops_unknown_keys(tmp_path):
    """舊存檔有已移除的欄位時不該 TypeError。"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({
        "current": 2,
        "milestones": {"1": {"phase": "done", "cost_usd": 9.0, "gone": True}},
    }), encoding="utf-8")
    st = PipelineState.load(p)
    assert st.current == 2
    assert st.ms(1).phase == "done"
    assert st.ms(1).cost_usd == 0.0  # 舊的 cost_usd 欄位被忽略


def test_cost_usd_is_not_persisted_as_a_field():
    # cost_usd 是 property,不該進 asdict,否則 load 時又要濾一次
    assert "cost_usd" not in MilestoneState().__dataclass_fields__


# -- config 驗證 -------------------------------------------------------------

def _cfg(tmp_path, **over):
    raw = {
        "repo": {"path": str(tmp_path), "base_branch": "main", "remote": "origin"},
        "plan": {"path": "plan.md"},
        "implementer": {"model": "fable"},
        "reviewer": {"model": "opus"},
        "loop": {},
    }
    for k, v in over.items():
        section, key = k.split(".", 1)
        raw.setdefault(section, {})[key] = v
    p = tmp_path / "pipeline.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return Config.load(p)


def test_config_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.implementer.permission_mode == "acceptEdits"
    # reviewer 不改 code,不該預設 acceptEdits
    assert cfg.reviewer.permission_mode == "default"
    assert cfg.loop.merge_method == "squash"
    assert cfg.plan_path == tmp_path / "plan.md"


@pytest.mark.parametrize("field,value", [
    ("loop.merge_method", "sqush"),
    ("reviewer.type", "action"),
    ("implementer.permission_mode", "yolo"),
])
def test_config_rejects_bad_enum(tmp_path, field, value):
    with pytest.raises(SystemExit):
        _cfg(tmp_path, **{field: value})


def test_config_rejects_missing_repo(tmp_path):
    raw = {
        "repo": {"path": str(tmp_path / "nope")},
        "plan": {"path": "plan.md"},
        "implementer": {"model": "fable"},
        "reviewer": {"model": "opus"},
    }
    p = tmp_path / "pipeline.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(SystemExit):
        Config.load(p)


# -- prompt 用了設定的 remote ------------------------------------------------

def test_prompts_use_configured_remote():
    from milestone_pipeline.prompts import fix_prompt, implement_prompt
    impl = implement_prompt("pre", "t", "b", "milestone/1-t", "main", 1,
                            remote="upstream")
    assert "git push -u upstream milestone/1-t" in impl
    assert "origin" not in impl

    fix = fix_prompt("fb", 3, 1, "milestone/1-t", remote="upstream")
    assert "git push upstream milestone/1-t" in fix
    assert "origin" not in fix


# -- 工具限制 ----------------------------------------------------------------

def test_reviewer_tools_exclude_write_tools():
    """`tools` 才是真正的白名單;reviewer 不該拿得到 Edit/Write。"""
    from milestone_pipeline.reviewer import REVIEWER_TOOLS
    assert "Edit" not in REVIEWER_TOOLS
    assert "Write" not in REVIEWER_TOOLS
    assert "Read" in REVIEWER_TOOLS


# -- UNRESOLVED 契約 ---------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("修好了\nUNRESOLVED: NO", False),
    ("有爭議\nUNRESOLVED: YES", True),
    ("  UNRESOLVED:YES  ", True),                        # 容忍空白
    ("UNRESOLVED: YES\nUNRESOLVED: NO", False),          # 取最後一個
    ("UNRESOLVED: NO\nUNRESOLVED: YES", True),
])
def test_parse_unresolved(text, expected):
    from milestone_pipeline.prompts import parse_unresolved
    assert parse_unresolved(text) is expected


def test_parse_unresolved_missing_marker_is_fail_open():
    """與 VERDICT 相反:解析不到時視為「沒有分歧」。

    下游還有 reviewer 的 APPROVE 擋著,而 agent 漏掉結尾標記很常見;
    若這裡 fail-closed,無人值守跑會每輪都停下來。
    """
    from milestone_pipeline.prompts import (has_unresolved_marker,
                                            parse_unresolved)
    assert parse_unresolved("完全沒提到標記") is False
    # 但呼叫端要能分辨「沒說」與「說了 NO」,才有辦法記警告
    assert has_unresolved_marker("完全沒提到標記") is False
    assert has_unresolved_marker("UNRESOLVED: NO") is True


def test_parse_unresolved_ignores_inline_mention():
    from milestone_pipeline.prompts import parse_unresolved
    text = ("我等一下會輸出 UNRESOLVED: YES 或 UNRESOLVED: NO。\n"
            "都處理完了。\n"
            "UNRESOLVED: NO")
    assert parse_unresolved(text) is False


def test_fix_prompt_declares_the_unresolved_contract():
    """契約的兩半必須同時存在,否則 orchestrator 永遠解析不到。"""
    from milestone_pipeline.prompts import fix_prompt
    p = fix_prompt("fb", 1, 1, "b")
    assert "UNRESOLVED: YES" in p
    assert "UNRESOLVED: NO" in p


# -- merge gate --------------------------------------------------------------

@pytest.mark.parametrize("gate,start,index,expected", [
    ("auto", 1, 5, False),      # auto 一律不問
    ("ask", 1, 1, True),
    ("ask", 3, 2, False),       # 還沒到起算的 milestone
    ("ask", 3, 3, True),
    ("ask", 3, 9, True),
])
def test_needs_human_merge(gate, start, index, expected):
    loop = LoopCfg(merge_gate=gate, merge_gate_from_milestone=start)
    assert loop.needs_human_merge(index) is expected


def test_config_merge_gate_defaults_to_auto(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.loop.merge_gate == "auto"
    assert cfg.loop.needs_human_merge(1) is False
    assert cfg.notify.channels == []


def test_config_rejects_bad_gate_and_channel(tmp_path):
    with pytest.raises(SystemExit):
        _cfg(tmp_path, **{"loop.merge_gate": "maybe"})
    with pytest.raises(SystemExit):
        _cfg(tmp_path, **{"notify.channels": ["telegram"]})


def test_config_webhook_channel_requires_url(tmp_path):
    """設定錯誤要在載入時就炸,不要等流程跑一小時才發現通知送不出去。"""
    with pytest.raises(SystemExit):
        _cfg(tmp_path, **{"notify.channels": ["webhook"]})


def test_config_accepts_single_channel_as_string(tmp_path):
    cfg = _cfg(tmp_path, **{"notify.channels": "pr_comment"})
    assert cfg.notify.channels == ["pr_comment"]


# -- 決策狀態 ----------------------------------------------------------------

def test_await_fields_survive_roundtrip(tmp_path):
    st = PipelineState()
    ms = st.ms(1)
    ms.phase = PH_AWAIT_HUMAN
    ms.await_reason = "merge_gate"
    ms.await_payload = "reviewer 已 APPROVE"
    ms.await_prev_phase = PH_MERGE
    ms.merge_approved = False
    st.save(tmp_path / "s.json")

    back = PipelineState.load(tmp_path / "s.json").ms(1)
    assert back.phase == PH_AWAIT_HUMAN
    assert back.await_reason == "merge_gate"
    assert back.await_prev_phase == PH_MERGE


def test_old_savefile_without_await_fields_still_loads(tmp_path):
    """加欄位必須給預設值,否則舊存檔會炸 —— 這是 state.py 的相容性約定。"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({
        "current": 1,
        "milestones": {"1": {"phase": "review", "pr_number": 3,
                             "review_round": 2}},
    }), encoding="utf-8")
    ms = PipelineState.load(p).ms(1)
    assert ms.pr_number == 3
    assert ms.await_reason is None
    assert ms.merge_approved is False
    assert ms.human_feedback is None


# -- 通知內容 ----------------------------------------------------------------

def _decision(**over):
    from milestone_pipeline.notify import R_MERGE_GATE, Decision
    kw = dict(milestone_index=3, milestone_title="CWA 天氣",
              reason=R_MERGE_GATE, detail="reviewer 已 APPROVE",
              config_hint="my.yaml", pr_number=7,
              pr_url="https://github.com/o/r/pull/7", cost_usd=12.5)
    kw.update(over)
    return Decision(**kw)


def test_decision_body_carries_everything_needed_to_decide():
    """通知要能讓人在手機上判斷:哪個 milestone、PR、為什麼停、怎麼恢復。"""
    body = _decision().body(mention="@SP42K")
    assert "Milestone 3" in body and "CWA 天氣" in body
    assert "https://github.com/o/r/pull/7" in body
    assert "@SP42K" in body
    assert "$12.50" in body
    assert "approve --milestone 3 --config my.yaml" in body
    assert "reject --milestone 3" in body


def test_decision_plain_has_no_markdown_fences():
    plain = _decision().plain()
    assert "```" not in plain
    assert "Milestone 3" in plain


def test_multi_notifier_swallows_channel_failures():
    """通知失敗絕不能中斷 pipeline —— 狀態已經存檔了。"""
    from milestone_pipeline.notify import MultiNotifier, Notifier

    class Boom(Notifier):
        def notify(self, decision):
            raise RuntimeError("webhook 掛了")

    seen = []

    class Ok(Notifier):
        def notify(self, decision):
            seen.append(decision.milestone_index)

    # 壞的排在前面,後面的仍要收到
    MultiNotifier([Boom(), Ok()]).notify(_decision())
    assert seen == [3]


def test_webhook_payload_shapes():
    from milestone_pipeline.notify import WebhookNotifier
    d = _decision()
    assert "content" in WebhookNotifier("u", "discord")._payload(d)
    assert "text" in WebhookNotifier("u", "slack")._payload(d)
    assert WebhookNotifier("u", "raw")._payload(d)["milestone"] == 3


def test_discord_payload_is_truncated():
    """Discord content 超過 2000 會 400,寧可截斷也不要整則掉。"""
    from milestone_pipeline.notify import WebhookNotifier
    d = _decision(detail="x" * 5000)
    payload = WebhookNotifier("u", "discord")._payload(d)
    assert len(payload["content"]) <= 1900


def test_make_notifier_returns_null_when_no_channels(tmp_path):
    from milestone_pipeline.config import NotifyCfg
    from milestone_pipeline.notify import NullNotifier, make_notifier
    assert isinstance(make_notifier(NotifyCfg(), tmp_path), NullNotifier)

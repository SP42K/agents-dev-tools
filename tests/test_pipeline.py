"""不需要 SDK / gh / 網路的純邏輯測試。"""
from __future__ import annotations

import json

import pytest
import yaml

from milestone_pipeline.config import Config
from milestone_pipeline.plan import Plan
from milestone_pipeline.state import (PH_MERGE, MilestoneState, PipelineState)


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
        raw[k.split(".")[0]][k.split(".")[1]] = v
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

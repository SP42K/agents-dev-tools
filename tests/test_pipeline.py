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


# -- hybrid reviewer:open-code-review 委託模式 -------------------------------

def test_config_accepts_hybrid_and_ocr_defaults(tmp_path):
    """舊 config(沒有任何 ocr_* 欄位)要拿得到可用的預設值。"""
    cfg = _cfg(tmp_path, **{"reviewer.type": "hybrid"})
    assert cfg.reviewer.type == "hybrid"
    assert cfg.reviewer.ocr_exe == "ocr"
    assert cfg.reviewer.ocr_timeout_sec == 300
    assert cfg.reviewer.ocr_exclude == ""
    assert cfg.reviewer.ocr_max_rule_chars == 40000


def test_config_reads_ocr_overrides(tmp_path):
    cfg = _cfg(tmp_path, **{
        "reviewer.type": "hybrid",
        "reviewer.ocr_exclude": "dist/,**/*.min.js",
        "reviewer.ocr_max_rule_chars": 1000,
    })
    assert cfg.reviewer.ocr_exclude == "dist/,**/*.min.js"
    assert cfg.reviewer.ocr_max_rule_chars == 1000


def _ocr(tmp_path):
    from milestone_pipeline.ocr import Ocr
    return Ocr(tmp_path)


def test_ocr_uses_delegate_not_review(tmp_path):
    """委託模式是刻意的:`ocr review` 要自己的 LLM 金鑰,delegate 不用。"""
    o = _ocr(tmp_path)
    assert o.preview_argv("main", "x")[1:3] == ["delegate", "preview"]
    assert o.rule_argv(["a.py"], "main", "x")[1:3] == ["delegate", "rule"]
    for argv in (o.preview_argv("main", "x"), o.rule_argv(["a.py"], "main", "x")):
        assert argv[argv.index("--format") + 1] == "json"
        assert argv[argv.index("--from") + 1] == "main"
        assert argv[argv.index("--to") + 1] == "x"


def test_ocr_rule_argv_puts_paths_last(tmp_path):
    """`delegate rule` 的路徑是位置參數,必須排在所有旗標之後。"""
    argv = _ocr(tmp_path).rule_argv(["a.py", "b/c.go"], "main", "x",
                                    rule_path="r.json")
    assert argv[-2:] == ["a.py", "b/c.go"]
    assert argv[argv.index("--rule") + 1] == "r.json"


def test_ocr_argv_uses_single_comma_separated_exclude(tmp_path):
    """`--exclude` 吃單一逗號字串,不是重複傳多次。"""
    argv = _ocr(tmp_path).preview_argv("main", "x", exclude="dist/,**/*.min.js")
    assert argv.count("--exclude") == 1
    assert argv[argv.index("--exclude") + 1] == "dist/,**/*.min.js"


def test_ocr_argv_omits_empty_optionals(tmp_path):
    argv = _ocr(tmp_path).preview_argv("main", "x", exclude="", rule_path=None)
    assert "--exclude" not in argv and "--rule" not in argv


def test_ocr_batches_paths_under_argv_budget():
    """40+ 檔的 milestone 不能一次塞進 argv,Windows 命令列有長度上限。"""
    from milestone_pipeline.ocr import Ocr
    paths = [f"pkg/module_{i:03d}.py" for i in range(40)]
    batches = Ocr.batch_paths(paths, budget=100)
    assert sum(batches, []) == paths          # 不漏、不重排
    assert all(sum(len(p) + 1 for p in b) <= 100 for b in batches[:-1])
    assert len(batches) > 1


def test_ocr_batch_keeps_oversized_path_rather_than_dropping_it():
    """單一路徑就超過預算時仍自成一批,寧可讓 subprocess 報錯也不要靜默丟檔。"""
    from milestone_pipeline.ocr import Ocr
    assert Ocr.batch_paths(["x" * 50], budget=10) == [["x" * 50]]


def test_ocr_resolves_windows_shim_and_falls_back(tmp_path):
    """npm 在 Windows 裝出來的是 ocr.CMD,subprocess 不做 PATHEXT 解析。"""
    import shutil
    from milestone_pipeline.ocr import Ocr
    # 解不到時原樣退回,讓 subprocess 自己丟 FileNotFoundError
    assert Ocr(tmp_path, exe="definitely-not-a-real-exe")._resolve_exe() \
        == "definitely-not-a-real-exe"
    # 解得到時要拿到 which 給的完整路徑(用一定存在的 python 當代理)
    assert Ocr(tmp_path, exe="python")._resolve_exe() == shutil.which("python")


# `ocr delegate preview --format json` 的實際輸出(v1.9.4,已精簡)
_PREVIEW_JSON = json.dumps({
    "schema_version": "1", "mode": "range", "merge_base": "b1dccac84a90",
    "total_files": 3, "reviewable_count": 2, "excluded_count": 1,
    "total_insertions": 472, "total_deletions": 7,
    "reviewable_files": [
        {"path": "milestone_pipeline/runner.py", "status": "modified",
         "insertions": 13, "deletions": 3},
        {"path": "formosa.yaml", "status": "modified",
         "insertions": 14, "deletions": 4},
    ],
    "excluded_files": [
        {"path": "docs/SKILL.md", "status": "added", "insertions": 356,
         "deletions": 0, "exclude_reason": "unsupported_ext"},
    ],
})

_RULE_JSON = json.dumps({
    "schema_version": "1",
    "groups": [{"group_id": 1, "source": "system", "pattern": "**/*.py",
                "files": ["milestone_pipeline/runner.py"],
                "rule": "#### Mutable Default Arguments\n- def f(x=[]) 很危險"}],
})


def test_ocr_parse_preview_maps_files_and_merge_base():
    from milestone_pipeline.ocr import Ocr
    plan = Ocr.parse_preview(_PREVIEW_JSON)
    assert plan.merge_base == "b1dccac84a90"
    assert plan.paths == ["milestone_pipeline/runner.py", "formosa.yaml"]
    assert plan.excluded[0]["exclude_reason"] == "unsupported_ext"
    assert (plan.total_insertions, plan.total_deletions) == (472, 7)


def test_ocr_parse_rules_maps_groups():
    from milestone_pipeline.ocr import Ocr
    groups = Ocr.parse_rules(_RULE_JSON)
    assert len(groups) == 1
    assert groups[0].pattern == "**/*.py"
    assert groups[0].files == ["milestone_pipeline/runner.py"]
    assert "Mutable Default" in groups[0].rule


@pytest.mark.parametrize("raw", ["", "   ", "not json", "[]", '{"groups": []}'])
def test_ocr_parse_preview_rejects_malformed(raw):
    from milestone_pipeline.ocr import Ocr, OcrError
    with pytest.raises(OcrError):
        Ocr.parse_preview(raw)


@pytest.mark.parametrize("raw", ["", "nope", "[]", '{"reviewable_files": []}'])
def test_ocr_parse_rules_rejects_malformed(raw):
    from milestone_pipeline.ocr import Ocr, OcrError
    with pytest.raises(OcrError):
        Ocr.parse_rules(raw)


def _plan_and_groups():
    from milestone_pipeline.ocr import Ocr
    return Ocr.parse_preview(_PREVIEW_JSON), Ocr.parse_rules(_RULE_JSON)


def test_format_review_plan_lists_every_reviewable_file():
    from milestone_pipeline.prompts import format_review_plan
    text = format_review_plan(*_plan_and_groups())
    assert "milestone_pipeline/runner.py" in text
    assert "formosa.yaml" in text
    assert "b1dccac84a90" in text


def test_format_review_plan_keeps_excluded_files_in_scope():
    """OCR 略過的檔仍在 diff 裡,不點名 reviewer 就會跟著漏掉。"""
    from milestone_pipeline.prompts import format_review_plan
    text = format_review_plan(*_plan_and_groups())
    assert "docs/SKILL.md" in text
    assert "unsupported_ext" in text
    assert "仍然要你自己看" in text


def test_format_review_plan_includes_rule_text():
    from milestone_pipeline.prompts import format_review_plan
    text = format_review_plan(*_plan_and_groups())
    assert "**/*.py" in text
    assert "Mutable Default" in text


def test_format_review_plan_announces_truncation():
    """靜默截斷會讓 reviewer 以為拿到了完整清單。"""
    from milestone_pipeline.prompts import format_review_plan
    plan, groups = _plan_and_groups()
    text = format_review_plan(plan, groups, max_rule_chars=20)
    assert "截斷" in text


def test_format_review_plan_never_truncates_the_file_lists():
    """截斷只能吃規則段。檔案清單是覆蓋範圍的下限,截掉就等於默許漏審。"""
    from milestone_pipeline.ocr import ReviewPlan, RuleGroup
    from milestone_pipeline.prompts import format_review_plan
    plan = ReviewPlan(
        merge_base="abc123def456",
        reviewable=[{"path": f"src/f{i}.py", "status": "modified",
                     "insertions": 5, "deletions": 1} for i in range(40)],
        excluded=[{"path": "docs/README.md", "exclude_reason": "unsupported_ext"}])
    groups = [RuleGroup(pattern="**/*.py", files=["src/f0.py"], rule="X" * 7000)]
    # 上限刻意設得比檔案清單本身還小
    text = format_review_plan(plan, groups, max_rule_chars=10)
    assert "src/f39.py" in text          # 最後一個待審檔仍在
    assert "docs/README.md" in text      # 被略過的檔仍被點名
    assert "仍然要你自己看" in text
    assert "截斷" in text


def test_format_review_plan_handles_empty_diff():
    from milestone_pipeline.ocr import ReviewPlan
    from milestone_pipeline.prompts import format_review_plan
    text = format_review_plan(ReviewPlan(), [])
    assert text.strip()
    assert "沒有可審的檔案" in text


def test_hybrid_prompt_satisfies_verdict_contract():
    """hybrid 模板與 review 模板要對同一個 _VERDICT_RE 負責。"""
    from milestone_pipeline.reviewer import _VERDICT_RE
    from milestone_pipeline.prompts import hybrid_review_prompt, review_prompt
    for text in (review_prompt(7, 1, "spec"),
                 hybrid_review_prompt(7, 1, "spec", "section")):
        assert _VERDICT_RE.findall(text) == ["APPROVE", "REQUEST_CHANGES"]


def test_hybrid_prompt_carries_plan_and_section():
    from milestone_pipeline.prompts import hybrid_review_prompt
    text = hybrid_review_prompt(7, 2, "MILESTONE-SPEC", "OCR-SECTION")
    assert "MILESTONE-SPEC" in text
    assert "OCR-SECTION" in text
    assert "#7" in text and "第 2 輪" in text


def test_hybrid_prompt_frames_list_as_lower_bound():
    """檔案清單是下限、規則是提醒 —— 不講清楚,reviewer 會照它的低召回取捨收斂。"""
    from milestone_pipeline.prompts import hybrid_review_prompt
    text = hybrid_review_prompt(7, 1, "spec", "section")
    assert "下限,不是上限" in text
    assert "不是收斂指令" in text


def test_hybrid_scan_fails_open_on_bad_gh_json(tmp_path):
    """gh 輸出壞掉時要 fail-open。JSONDecodeError 繼承 ValueError 不是 RuntimeError,
    漏掉它 fail-open 就破功,整條 pipeline 會炸。"""
    from milestone_pipeline.config import ReviewerCfg
    from milestone_pipeline.reviewer import HybridReviewer
    r = HybridReviewer(ReviewerCfg(model="opus", type="hybrid"), tmp_path, "master")
    r.gh.pr_view = lambda *a, **k: (_ for _ in ()).throw(
        json.JSONDecodeError("boom", "", 0))
    section = r._scan(1)
    assert "沒有跑成功" in section


def test_ocr_unavailable_note_names_the_reason():
    """OCR 失敗是 fail-open,但原因必須傳到 reviewer 眼前,不能靜默。"""
    from milestone_pipeline.prompts import ocr_unavailable_note
    note = ocr_unavailable_note("找不到 `ocr`")
    assert "找不到 `ocr`" in note
    assert "逐一審過" in note



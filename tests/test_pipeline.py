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


def test_implementer_cost_overwrites_within_one_session():
    """同一個 session 內 SDK 給的是累計值,所以要覆寫而不是累加。"""
    ms = MilestoneState()
    ms.rebase_implementer_cost()
    ms.record_implementer_cost(3.0)
    ms.record_implementer_cost(8.0)   # 同一個 session 的新累計值
    assert ms.implementer_cost_usd == 8.0


def test_implementer_cost_survives_resume():
    """resume 開新 session、SDK 從 0 重算,舊花費不能被蓋掉。

    實測 formosa milestone 3:crash 前 $25.54,resume 後 SDK 回報 $11.07,
    舊行為直接覆寫 → 顯示 $11.07,少報了 $25.54。
    """
    ms = MilestoneState()
    ms.rebase_implementer_cost()
    ms.record_implementer_cost(25.54)          # 第一個 process

    ms.rebase_implementer_cost()               # crash 後重跑,進 milestone
    ms.record_implementer_cost(4.0)            # 新 session 的累計值
    assert ms.implementer_cost_usd == pytest.approx(29.54)
    ms.record_implementer_cost(11.07)          # 同一個新 session 再回報
    assert ms.implementer_cost_usd == pytest.approx(36.61)


def test_cost_base_roundtrips_and_old_savefile_defaults_to_zero(tmp_path):
    """新欄位有預設值,舊存檔載得起來(加必填欄位才會不相容)。"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps({
        "current": 1,
        "milestones": {"1": {"phase": "review", "implementer_cost_usd": 5.0}},
    }), encoding="utf-8")
    ms = PipelineState.load(p).ms(1)
    assert ms.implementer_cost_base_usd == 0.0
    assert ms.implementer_cost_usd == 5.0

    ms.rebase_implementer_cost()
    ms.record_implementer_cost(2.0)
    st = PipelineState()
    st.milestones["1"] = ms
    st.save(p)
    assert PipelineState.load(p).ms(1).implementer_cost_usd == pytest.approx(7.0)


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


# -- SDK 例外的 park 內容 ----------------------------------------------------

def test_agent_crash_detail_names_the_phase_and_the_exception():
    """實測過的那顆:SDK 控制平面丟例外,不是 agent 回報的 is_error。"""
    from milestone_pipeline.orchestrator import agent_crash_detail
    exc = Exception("Claude Code returned an error result: success")
    detail = agent_crash_detail("review", exc)
    assert "review" in detail
    assert "Exception" in detail                       # 例外型別
    assert "returned an error result" in detail        # 原始訊息
    assert "approve" in detail                         # 怎麼繼續


def test_agent_crash_detail_truncates_giant_exceptions():
    """通知通道(Discord 2000 字)與存檔都吃不下無上限的訊息。"""
    from milestone_pipeline.orchestrator import agent_crash_detail
    detail = agent_crash_detail("實作", RuntimeError("x" * 10_000))
    assert detail.count("x") == 2000

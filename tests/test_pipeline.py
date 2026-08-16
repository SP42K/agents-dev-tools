"""不需要 SDK / gh / 網路的純邏輯測試。"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys

import pytest
import yaml

from milestone_pipeline.backend import AgentBackend, AgentSession
from milestone_pipeline.config import AgentCfg, Config, LoopCfg, ReviewerCfg
from milestone_pipeline.implementer import IMPLEMENTER_TOOLS, Implementer
from milestone_pipeline.plan import Plan
from milestone_pipeline.prompts import verify_fail_prompt
from milestone_pipeline.reviewer import REVIEWER_TOOLS, ScriptReviewer
from milestone_pipeline.runner import AgentResult
from milestone_pipeline.state import (PH_AWAIT_HUMAN, PH_MERGE, MilestoneState,
                                      PipelineState)
from milestone_pipeline.verify import (fingerprint, run_verify,
                                       workspace_fingerprint)

# 驗收測試要一個「兩個平台都一定跑得起來」的命令。**不要寫 `python`** ——
# macOS 上沒有那支(只有 `python3`),`shell=True` 會直接回 127。
# 引號是給路徑有空白的情況用的,cmd.exe 與 sh 都認。
PY = f'"{sys.executable}"'


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
        _cfg(tmp_path, **{"notify.channels": ["carrier-pigeon"]})


def test_config_webhook_channel_requires_url(tmp_path):
    """設定錯誤要在載入時就炸,不要等流程跑一小時才發現通知送不出去。"""
    with pytest.raises(SystemExit):
        _cfg(tmp_path, **{"notify.channels": ["webhook"]})


def test_config_telegram_missing_creds_do_not_block_startup(tmp_path, monkeypatch):
    """token / chat id 都不炸 —— 兩個都來自環境變數,而同一份 config 會在不同機器起。

    通知是旁路,一台沒設環境變數的機器不該連 pipeline 都起不來。降級在
    make_notifier()。`webhook_url` 是相反的:它只能寫在 config 裡,缺了就是打錯字。
    """
    from milestone_pipeline.config import TELEGRAM_CHAT_ID_ENV, TELEGRAM_TOKEN_ENV

    monkeypatch.delenv(TELEGRAM_TOKEN_ENV, raising=False)
    monkeypatch.delenv(TELEGRAM_CHAT_ID_ENV, raising=False)
    cfg = _cfg(tmp_path, **{"notify.channels": ["telegram"]})
    assert cfg.notify.telegram_token == ""
    assert cfg.notify.telegram_chat_id == ""


def test_config_telegram_creds_fall_back_to_env(tmp_path, monkeypatch):
    """兩個都不必寫進 yaml —— token 是密鑰,chat id 是永久的個人識別碼,repo 是公開的。"""
    from milestone_pipeline.config import TELEGRAM_CHAT_ID_ENV, TELEGRAM_TOKEN_ENV

    monkeypatch.setenv(TELEGRAM_TOKEN_ENV, "from-env")
    monkeypatch.setenv(TELEGRAM_CHAT_ID_ENV, "854590099")
    cfg = _cfg(tmp_path, **{"notify.channels": ["telegram"]})
    assert cfg.notify.telegram_token == "from-env"
    assert cfg.notify.telegram_chat_id == "854590099"

    # yaml 寫了就以 yaml 為準,且數字要轉成字串(urllib 送 JSON 時字串才安全)
    cfg = _cfg(tmp_path, **{"notify.channels": ["telegram"],
                            "notify.telegram_chat_id": 12345})
    assert cfg.notify.telegram_chat_id == "12345"


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
    # 解得到時要拿到 which 給的完整路徑(用 `git` 當代理:兩個平台都一定有,
    # 而 macOS 上沒有 `python` 這支 —— 只有 `python3`)
    assert Ocr(tmp_path, exe="git")._resolve_exe() == shutil.which("git")


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


def test_round_notes_keys_on_reviewer_seen_not_round_number():
    """鎖範圍的前提是 reviewer 真的掃過,不是「輪數 > 1」。

    人工 reject 會吃掉一輪卻跳過 reviewer,所以 round_no 不是合法的代理 ——
    用它的話 reviewer 一出場就被鎖在「人的意見」那個範圍裡。
    """
    from milestone_pipeline.prompts import round_notes
    assert round_notes(False, False) == ""
    assert "這一輪的範圍" in round_notes(True, False)
    assert "這是最後一輪" not in round_notes(True, False)
    # 第五項 blocker:實測最有價值的發現都是這個形狀,fixtures 與 typecheck
    # 都測不出來。少了它,_SCOPE_LOCK 會把它降級成沒人會做的「後續建議」。
    assert "對外承諾與實際行為不符" in round_notes(True, False)
    both = round_notes(True, True)
    assert "這一輪的範圍" in both and "這是最後一輪" in both
    # 最後一輪但 reviewer 還沒掃過(reject 後上限只剩一輪):不鎖範圍,只講最後一輪
    only = round_notes(False, True)
    assert "這一輪的範圍" not in only and "這是最後一輪" in only


def test_review_prompts_carry_round_notes():
    """兩個模板都要吃到收斂段落 —— 只改一個就等於 hybrid 模式沒有收斂機制。"""
    from milestone_pipeline.prompts import hybrid_review_prompt, review_prompt
    for text in (review_prompt(7, 3, "spec", reviewer_seen=True, is_final=True),
                 hybrid_review_prompt(7, 3, "spec", "section",
                                      reviewer_seen=True, is_final=True)):
        assert "這一輪的範圍" in text
        assert "這是最後一輪" in text
    # 第 3 輪但 reviewer 沒掃過(reject 吃掉前兩輪)→ 一樣要拿到完整掃描
    for text in (review_prompt(7, 3, "spec"),
                 hybrid_review_prompt(7, 3, "spec", "section")):
        assert "這一輪的範圍" not in text
        assert "這是最後一輪" not in text


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


def test_telegram_payload_and_token_stays_out_of_errors():
    from milestone_pipeline.notify import TelegramNotifier
    n = TelegramNotifier("SECRET-TOKEN", "12345", mention="@SP42K")
    payload = n._payload(_decision(detail="y" * 9000))
    assert payload["chat_id"] == "12345"
    assert "Milestone 3" in payload["text"]
    # 4096 是 sendMessage 的硬上限,超過整則會被拒
    assert len(payload["text"]) <= 4000
    # parse_mode 不能設:body 裡的 `-` `.` `(` 會讓 MarkdownV2 回 400
    assert "parse_mode" not in payload
    # token 在 URL 路徑裡,絕不能出現在 payload 或錯誤訊息中
    assert "SECRET-TOKEN" not in json.dumps(payload)


def test_applescript_str_escapes_in_the_right_order():
    """反斜線要先跳脫 —— 顛倒的話補進去的 `\\"` 會被再跳脫一次,引號就漏出來。"""
    from milestone_pipeline.notify import _applescript_str
    assert _applescript_str('a"b') == '"a\\"b"'
    assert _applescript_str("a\\b") == '"a\\\\b"'
    # 反斜線後面接引號:最容易踩的組合
    assert _applescript_str('a\\"b') == '"a\\\\\\"b"'
    # AppleScript 字串常值不能含真正的換行
    assert "\n" not in _applescript_str("a\nb")


def test_make_notifier_returns_null_when_no_channels(tmp_path):
    from milestone_pipeline.config import NotifyCfg
    from milestone_pipeline.notify import NullNotifier, make_notifier
    assert isinstance(make_notifier(NotifyCfg(), tmp_path), NullNotifier)


def test_make_notifier_skips_telegram_without_token(tmp_path):
    """沒 token 就不裝這個 channel —— 不要裝了之後每次 park 都 401。"""
    from milestone_pipeline.config import NotifyCfg
    from milestone_pipeline.notify import (MultiNotifier, NullNotifier,
                                           TelegramNotifier, make_notifier)

    cfg = NotifyCfg(channels=["telegram"], telegram_chat_id="1")
    assert isinstance(make_notifier(cfg, tmp_path), NullNotifier)

    cfg.telegram_token = "t"
    n = make_notifier(cfg, tmp_path)
    assert isinstance(n, MultiNotifier)
    assert isinstance(n.children[0], TelegramNotifier)


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


# -- verify gate(確定性驗收關卡)---------------------------------------------

def test_config_verify_defaults(tmp_path):
    """沒設就是不跑 —— 舊的 pipeline.yaml / formosa.yaml 不改也要能跑。"""
    cfg = _cfg(tmp_path)
    assert cfg.loop.verify_command == ""
    assert cfg.loop.verify_timeout_sec == 900


def test_config_reads_verify_overrides(tmp_path):
    cfg = _cfg(tmp_path, **{"loop.verify_command": "pytest && ruff check .",
                            "loop.verify_timeout_sec": 120})
    assert cfg.loop.verify_command == "pytest && ruff check ."
    assert cfg.loop.verify_timeout_sec == 120


def test_verify_skipped_when_no_command(tmp_path):
    """skipped 時 ok 也是 True —— 呼叫端只要看 .ok。"""
    r = run_verify("   ", tmp_path)
    assert (r.ok, r.skipped) == (True, True)


def test_verify_success(tmp_path):
    r = run_verify(f'{PY} -c "print(\'ok\')"', tmp_path, timeout_sec=60)
    assert r.ok is True
    assert r.skipped is False
    assert "ok" in r.output


def test_verify_failure_is_not_ok(tmp_path):
    # Windows 上 `false` 不存在,用直譯器比較保險
    r = run_verify(f'{PY} -c "raise SystemExit(1)"', tmp_path, timeout_sec=60)
    assert r.ok is False
    assert r.returncode == 1


def test_verify_shell_chaining_short_circuits(tmp_path):
    """shell=True 是刻意的:使用者要能寫 `a && b`。"""
    r = run_verify(f'{PY} -c "raise SystemExit(1)" && {PY} -c "print(1)"',
                   tmp_path, timeout_sec=60)
    assert r.ok is False


def test_verify_truncates_output_from_the_tail(tmp_path):
    """錯誤訊息通常在最後,而且靜默截斷會讓 implementer 以為拿到全文。"""
    r = run_verify(
        f'{PY} -c "print(\'a\'*200 + \'TAIL_MARKER\')"',
        tmp_path, timeout_sec=60, max_output_chars=50)
    assert "TAIL_MARKER" in r.output
    assert "截斷" in r.output
    assert len(r.output) < 200


def test_verify_fail_prompt_names_the_command_and_output():
    text = verify_fail_prompt("pytest -q", "E   assert 1 == 2")
    assert "pytest -q" in text
    assert "assert 1 == 2" in text
    # 一定要講清楚這不是 reviewer 說的,否則 implementer 會去 PR 上找對應意見
    assert "reviewer" in text
    # 不新增契約:UNRESOLVED 仍由 fix_prompt 負責,這裡不該自己要求
    assert "UNRESOLVED" not in text


# -- workspace 指紋(沒變就不重跑失敗的 gate)---------------------------------

def test_fingerprint_is_stable_and_changes_with_any_part():
    base = fingerprint("head", "status", "diff")
    assert base == fingerprint("head", "status", "diff")
    assert base != fingerprint("head2", "status", "diff")
    assert base != fingerprint("head", "status2", "diff")
    assert base != fingerprint("head", "status", "diff2")


def test_fingerprint_does_not_collide_on_boundary_shifts():
    """分隔字元不能是可能出現在 git 輸出裡的東西,否則兩段的邊界會糊掉。"""
    assert fingerprint("ab", "c") != fingerprint("a", "bc")


def test_workspace_fingerprint_returns_empty_outside_git_repo(tmp_path):
    """取不到就回空字串 → 呼叫端照跑驗收(退化方向安全)。"""
    assert workspace_fingerprint(tmp_path) == ""


# -- AgentBackend 介面 --------------------------------------------------------

def test_config_backend_defaults_to_claude(tmp_path):
    assert _cfg(tmp_path).backend == "claude"


def test_config_rejects_unknown_backend(tmp_path):
    raw = {
        "repo": {"path": str(tmp_path)},
        "plan": {"path": "plan.md"},
        "implementer": {"model": "fable"},
        "reviewer": {"model": "opus"},
        "backend": "prime-agent",
    }
    p = tmp_path / "pipeline.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(SystemExit):
        Config.load(p)


class _FakeSession(AgentSession):
    """假的**自家介面**,不是 SDK 型別 —— 所以沒有違反「不 mock SDK」。"""

    def __init__(self, spec, resume_session_id=None):
        self.spec = spec
        self.session_id = resume_session_id
        self.prompts: list[str] = []
        self.compacted = 0
        self.echoed: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def ask(self, prompt, on_text=None):
        self.prompts.append(prompt)
        if on_text is not None:
            on_text("agent 說話了")
            self.echoed.append("agent 說話了")
        # AgentSession 的契約:回報的新 session_id 要記在 session 上
        self.session_id = "sess-新"
        return AgentResult(text="做完了", session_id="sess-新", cost_usd=1.5)

    async def compact(self):
        self.compacted += 1


class _FakeBackend(AgentBackend):
    def __init__(self):
        self.last: _FakeSession | None = None

    def session(self, spec, resume_session_id=None):
        self.last = _FakeSession(spec, resume_session_id)
        return self.last

    async def query_once(self, spec, prompt, on_text=None):
        return AgentResult(text="VERDICT: APPROVE")


def _implementer(tmp_path, backend, resume=None):
    return Implementer(AgentCfg(model="fable"), tmp_path, backend,
                       resume_session_id=resume)


def test_implementer_runs_against_a_fake_backend(tmp_path):
    backend = _FakeBackend()

    async def go():
        async with _implementer(tmp_path, backend, resume="sess-舊") as imp:
            assert imp.session_id == "sess-舊"       # resume 先接上
            result = await imp.ask("實作 milestone 1")
            await imp.compact()
            return result, imp.session_id

    result, session_id = asyncio.run(go())
    assert result.text == "做完了"
    assert result.cost_usd == 1.5
    # Implementer.session_id 必須是**轉發** session 的值,不能自己 cache 一份
    # ——快取的話 orchestrator 存進 state 的會是舊的,crash 後 resume 接錯 session
    assert session_id == "sess-新"
    assert backend.last.prompts == ["實作 milestone 1"]
    assert backend.last.compacted == 1
    # ask 要接 on_text(即時輸出),compact 刻意不接(是雜訊)
    assert backend.last.echoed == ["agent 說話了"]


def test_implementer_spec_carries_the_tool_whitelist(tmp_path):
    """`tools` 才是白名單。清單經由 AgentSpec 傳給 backend,不能在路上掉了。"""
    backend = _FakeBackend()
    _implementer(tmp_path, backend)
    assert backend.last.spec.tools == IMPLEMENTER_TOOLS
    assert backend.last.spec.system_prompt.startswith("你是這個 repo 的 implementer")


def test_script_reviewer_uses_the_injected_backend(tmp_path):
    rev = ScriptReviewer(ReviewerCfg(model="opus"), tmp_path, _FakeBackend())
    out = asyncio.run(rev.review(7, 1, "## M1"))
    assert out.approved is True
    # reviewer 的 spec 不能帶 Edit/Write
    assert rev.spec.tools == REVIEWER_TOOLS


# -- verify gate 在 orchestrator 裡的接線(含指紋快取)-------------------------

def _git_repo(path):
    """在 tmp_path 起一個真的 git repo。不碰網路 / gh / SDK。"""
    for argv in (["git", "init", "-b", "m1"],
                 ["git", "config", "user.email", "t@example.com"],
                 ["git", "config", "user.name", "t"],
                 ["git", "add", "-A"],
                 ["git", "commit", "-m", "init", "--no-gpg-sign"]):
        subprocess.run(argv, cwd=path, check=True, capture_output=True)


def _orchestrator(tmp_path, **over):
    from milestone_pipeline.orchestrator import Orchestrator
    (tmp_path / "plan.md").write_text("intro\n\n## M1\nbody\n", encoding="utf-8")
    return Orchestrator(_cfg(tmp_path, **over))


def test_final_round_note_requires_a_human_merge_gate(tmp_path):
    """`_FINAL_ROUND` 是「非 blocker 一律放行」,所以 merge_gate=auto 時不能講 ——
    否則輪數用完會自動 merge,等於把 PH_STUCK 這道 fail-closed 換成 fail-open。"""
    ms = MilestoneState(branch="m1")

    orch = _orchestrator(tmp_path, **{"loop.max_review_rounds": 2,
                                      "loop.merge_gate": "ask"})
    m = orch.plan.milestones[0]
    ms.review_round = 1
    assert orch._is_final_round(m, ms) is False   # 還有下一輪
    ms.review_round = 2
    assert orch._is_final_round(m, ms) is True    # 輪數到頂 + 人還在把關

    auto = _orchestrator(tmp_path, **{"loop.max_review_rounds": 2,
                                      "loop.merge_gate": "auto"})
    assert auto._is_final_round(auto.plan.milestones[0], ms) is False

    # 第二種「人其實不在關卡上」:gate=ask 但這個 milestone 還沒進入 gate 範圍
    late = _orchestrator(tmp_path, **{"loop.max_review_rounds": 2,
                                      "loop.merge_gate": "ask",
                                      "loop.merge_gate_from_milestone": 3})
    assert late._is_final_round(late.plan.milestones[0], ms) is False


def test_verify_gate_skips_when_unconfigured(tmp_path):
    orch = _orchestrator(tmp_path)
    ms = MilestoneState(branch="m1")
    # 沒設命令就不該碰 git,所以連 repo 都不用是 git repo
    r = orch._verify(orch.plan.milestones[0], ms)
    assert (r.ok, r.skipped) == (True, True)


def test_verify_gate_reuses_output_until_the_workspace_changes(tmp_path, caplog):
    marker = tmp_path.parent / "verify-runs.txt"   # 放 repo 外,免得自己改變指紋
    marker.unlink(missing_ok=True)
    cmd = (f'{PY} -c "open(r\'{marker}\',\'a\').write(\'x\'); '
           'print(\'boom\'); raise SystemExit(1)"')
    orch = _orchestrator(tmp_path, **{"loop.verify_command": cmd})
    _git_repo(tmp_path)
    m, ms = orch.plan.milestones[0], MilestoneState(branch="m1")

    first = orch._verify(m, ms)
    assert first.ok is False
    assert "boom" in first.output
    assert marker.read_text() == "x"
    assert ms.last_verify_fingerprint                      # 失敗才存指紋

    # 第二輪 implementer 什麼都沒改 → 不重跑,沿用輸出,並記警告
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        second = orch._verify(m, ms)
    assert second.ok is False
    assert second.output == first.output
    assert marker.read_text() == "x"                       # 命令沒有再跑一次
    assert any("沒有變動" in r.getMessage() for r in caplog.records)

    # workspace 一變動就要重跑
    (tmp_path / "new.py").write_text("x", encoding="utf-8")
    assert orch._verify(m, ms).ok is False
    assert marker.read_text() == "xx"


def test_verify_gate_clears_the_cache_after_a_pass(tmp_path):
    orch = _orchestrator(tmp_path, **{"loop.verify_command": f"{PY} -c pass"})
    _git_repo(tmp_path)
    ms = MilestoneState(branch="m1",
                        last_verify_fingerprint="舊", last_verify_output="舊輸出")
    assert orch._verify(orch.plan.milestones[0], ms).ok is True
    # 沒清掉的話,下個 milestone 的第一次失敗會被誤判成「沒動作」
    assert ms.last_verify_fingerprint is None
    assert ms.last_verify_output is None


def test_workspace_fingerprint_can_ignore_our_own_artifacts(tmp_path):
    """state 檔每輪都被 _save() 改寫,不排掉的話「沒變動就不重跑」永遠不會生效。"""
    from milestone_pipeline.verify import workspace_fingerprint as fp_of
    (tmp_path / "seed.txt").write_text("x", encoding="utf-8")   # 要有東西才 commit 得起來
    _git_repo(tmp_path)
    state = tmp_path / ".pipeline-state.json"

    state.write_text("{}", encoding="utf-8")
    before = fp_of(tmp_path, ignore=[".pipeline-state.json"])
    state.write_text('{"current": 2}', encoding="utf-8")
    assert fp_of(tmp_path, ignore=[".pipeline-state.json"]) == before
    assert fp_of(tmp_path) != before          # 不排除的話就會被它帶著跑

    # 真的 code 變動仍然要看得到
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    assert fp_of(tmp_path, ignore=[".pipeline-state.json"]) != before


def test_make_backend_refuses_a_name_it_has_not_wired_up():
    """config.BACKENDS 與 make_backend 是兩個要一起改的地方。

    只加前者(加了新 backend 卻忘了接 dispatch)不能靜默落到 claude ——
    backend 決定工具白名單與預算閘門,跑錯 runtime 不會有症狀,只會失去保證。
    """
    from milestone_pipeline.backend import ClaudeBackend, make_backend
    assert isinstance(make_backend("claude"), ClaudeBackend)
    with pytest.raises(SystemExit):
        make_backend("prime-agent")


# -- 守護 agent ---------------------------------------------------------------

def test_guard_argv_carries_every_write_deny():
    """守護 agent 的價值全在「有沒有真的帶上 deny」,不能只靠人肉核對啟動指令。

    實測過一次沒帶的後果:守護 agent 在 merge gate 上自己 commit 進分支,
    那個 commit 沒經過 reviewer、沒經過 verify,merge 時 CI 還在跑。
    """
    from milestone_pipeline.guard import _DENY, build_argv
    argv = build_argv("formosa.yaml", claude_exe="/usr/bin/claude")

    assert argv[0] == "/usr/bin/claude"
    flag = argv.index("--disallowed-tools")
    for tool in _DENY:
        assert tool in argv[flag + 1:], f"{tool} 沒有被關掉"

    # 產生檔案與讓檔案生效是兩段,兩段都要擋:`--disallowed-tools Edit Write`
    # 擋不住 Bash 裡的 `cat > file`,但沒有 commit / push 就進不了 PR。
    assert "Write" in _DENY and "Bash(git commit:*)" in _DENY

    # 沒有人會去按「允許」——ask 模式下第一個非 allowlist 的命令就掛住整條
    # pipeline。deny 規則優先權更高,所以邊界沒有跟著鬆掉。
    mode = argv.index("--permission-mode")
    assert argv[mode + 1] == "bypassPermissions"


def test_guard_argv_wraps_with_unsnooze_only_when_present():
    """unsnooze 是選用的,而且必須是 per-session 的包法(不是全域 hook)。

    Windows 沒有 tmux,unsnooze 跑不起來 —— 那是預期中的降級,不是錯誤,
    所以沒有它的時候要照樣組得出一個可以跑的命令列。
    """
    from milestone_pipeline.guard import build_argv
    plain = build_argv("c.yaml", claude_exe="claude")
    wrapped = build_argv("c.yaml", claude_exe="claude", unsnooze_exe="unsnooze")

    assert plain[0] == "claude"
    assert wrapped[0] == "unsnooze" and wrapped[1:] == plain


def test_guard_spots_an_unaccepted_bypass_prompt(tmp_path):
    """沒接受過 bypassPermissions 的機器上,守護 agent 會停在同意對話框上。

    那正是 `--permission-mode` 要修的病本身,而 tmux detached 看不到 —— 所以
    起飛前要講一聲。讀不到 / 壞掉一律當成沒接受過(漏警告比多警告貴)。
    """
    from milestone_pipeline.guard import has_accepted_bypass

    f = tmp_path / ".claude.json"
    assert has_accepted_bypass(f) is False           # 檔案不存在
    f.write_text("{ not json", encoding="utf-8")
    assert has_accepted_bypass(f) is False           # 壞掉
    f.write_text('{"bypassPermissionsModeAccepted": false}', encoding="utf-8")
    assert has_accepted_bypass(f) is False
    f.write_text('{"bypassPermissionsModeAccepted": true}', encoding="utf-8")
    assert has_accepted_bypass(f) is True


def test_guard_spots_a_global_unsnooze_install(tmp_path):
    """全域 hook 會讓 unsnooze 對機器上每個 session 生效,不只守護 agent。

    讀不到 / 壞掉的 settings 一律當成沒裝 —— 這只是提醒,不該擋住啟動。
    """
    from milestone_pipeline.guard import has_global_unsnooze_hook
    f = tmp_path / "settings.json"

    f.write_text(json.dumps({"hooks": {"StopFailure": [
        {"hooks": [{"command": "node .../unsnooze/bin/unsnooze.js _hook"}]}]}}),
        encoding="utf-8")
    assert has_global_unsnooze_hook(f)

    f.write_text(json.dumps({"hooks": {}, "tui": "fullscreen"}), encoding="utf-8")
    assert not has_global_unsnooze_hook(f)

    f.write_text("{ not json", encoding="utf-8")
    assert not has_global_unsnooze_hook(f)
    assert not has_global_unsnooze_hook(tmp_path / "nope.json")


def test_guardian_system_prompt_states_the_reject_over_fix_rule():
    """這段文字要能獨自撐住一次「被用量上限打斷後醒來」。

    prompt 不是防線(防線是 _DENY),但它是守護 agent 唯一知道「為什麼」的地方 ——
    少了理由,下一任一樣會算出「直接修比較省」那個結論。
    """
    from milestone_pipeline.prompts import GUARDIAN_SYSTEM, guardian_task
    assert "reject" in GUARDIAN_SYSTEM and "一行" in GUARDIAN_SYSTEM
    assert "agent_error" in GUARDIAN_SYSTEM and "merge_gate" in GUARDIAN_SYSTEM
    # 醒來時不能憑記憶接續,狀態以存檔為準
    assert "status" in GUARDIAN_SYSTEM
    # 決策完沒重啟 run 的話,整條 pipeline 就停在那裡
    assert "run" in guardian_task("formosa.yaml")
    assert "formosa.yaml" in guardian_task("formosa.yaml")


def test_guard_session_name_is_a_stable_identity_tmux_accepts():
    """一個名字同時是身分、存活證明、與 unsnooze 打字的目標,所以要穩定且合法。

    tmux 的 session 名字不吃 `.` 與 `:`,而 config 檔名一定有 `.yaml`。
    """
    from milestone_pipeline.guard import session_name
    assert session_name("formosa.yaml") == "guard-formosa"
    assert session_name("/x/y/my-pipeline.v2.yaml") == "guard-my-pipeline-v2"
    for bad in (".", ":"):
        assert bad not in session_name("a.b:c.yaml")
    # 推不出東西時也要給得出一個合法名字,不能回 "guard-"
    assert session_name(".yaml").startswith("guard-")


def test_guard_repo_warnings_flag_dirty_tree_not_branch_name(tmp_path):
    """要防的是「跑到的 orchestrator 不是 git 裡那份」,不是「不在 master 上」。

    分支名不是 code 乾不乾淨的代理 —— 在 feature branch 上開發這條 pipeline
    本來就是正常的(`guard` 自己就是這樣寫出來的)。
    """
    from milestone_pipeline.guard import repo_warnings
    (tmp_path / "seed.txt").write_text("x", encoding="utf-8")
    _git_repo(tmp_path)
    assert repo_warnings(tmp_path) == []          # 乾淨的 repo,分支叫 m1 也不該警告

    (tmp_path / "seed.txt").write_text("changed", encoding="utf-8")
    warned = repo_warnings(tmp_path)
    assert len(warned) == 1 and "未 commit" in warned[0]

    # 不是 git repo(或 git 不在)不該炸,只是沒東西可警告
    assert repo_warnings(tmp_path / "nope") == []


def test_run_lock_refuses_a_second_holder(tmp_path):
    """兩個 run 併跑不會噴錯,只會互相覆蓋存檔 —— 所以要在入口擋掉。

    用 OS 檔案鎖而不是 pid 檔:crash / kill -9 之後 OS 自己放掉,不留殘骸。
    """
    from milestone_pipeline import lock
    state = tmp_path / ".pipeline-state.json"

    with lock.exclusive(state):
        with pytest.raises(SystemExit):
            with lock.exclusive(state):
                pass

    # 前一個放掉之後要拿得到 —— 鎖不能是一次性的
    with lock.exclusive(state):
        pass


def test_lock_file_is_excluded_from_the_workspace_fingerprint(tmp_path):
    """鎖檔是未追蹤的新檔案,落在目標 repo 裡就會弄髒 `status --porcelain`。

    不排掉的話:指紋每次都不一樣(「沒變動就不重跑」失效),而且 reviewer 與
    merge gate 的「`git status` 乾淨」也會被它汙染。
    """
    from milestone_pipeline import lock
    (tmp_path / "seed.txt").write_text("x", encoding="utf-8")
    _git_repo(tmp_path)
    state = tmp_path / ".pipeline-state.json"
    ignore = [state.name, lock.lock_path(state).name]

    before = workspace_fingerprint(tmp_path, ignore=ignore)
    with lock.exclusive(state):
        assert lock.lock_path(state).exists()
        assert workspace_fingerprint(tmp_path, ignore=ignore) == before
        # 沒排掉的話它確實看得見 —— 證明這個忽略項不是多餘的
        assert workspace_fingerprint(tmp_path, ignore=[state.name]) != before

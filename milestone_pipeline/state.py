"""進度存檔:讓 orchestrator crash / 中斷後可以 resume。

存的內容刻意極簡:目前做到哪個 milestone、哪個 phase、PR 編號、
implementer 的 session_id(用來 resume 同一個 context window)、review 輪數。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# phases
PH_IMPLEMENT = "implement"      # 尚未實作 / 實作中
PH_REVIEW = "review"            # PR 已開,在 review ↔ fix 迴圈中
PH_MERGE = "merge"              # 已 approve,待 merge
PH_DONE = "done"                # 此 milestone 完成
PH_STUCK = "stuck"              # 超過輪數上限,等人介入
PH_AWAIT_HUMAN = "await_human"  # 停在決策點等人,用 approve / reject 恢復


@dataclass
class MilestoneState:
    phase: str = PH_IMPLEMENT
    pr_number: int | None = None
    branch: str | None = None
    session_id: str | None = None   # implementer 持久 session
    review_round: int = 0
    # implementer 是「單一 session 的累計花費」(SDK 每次回傳都是累計值),
    # reviewer 是「每輪各自獨立」所以要自己加總。兩者語意不同,分開存。
    implementer_cost_usd: float = 0.0
    reviewer_cost_usd: float = 0.0
    # -- 人工決策(phase == PH_AWAIT_HUMAN 時才有意義)---------------------
    # 新增欄位一律給預設值:state.load 會濾掉不認得的 key,所以「刪欄位」相容,
    # 「加必填欄位」不相容(舊存檔會拿到 dataclass 預設值)。
    await_reason: str | None = None      # notify.R_* 之一
    await_payload: str | None = None     # 停下來當下要給人看的內容
    await_prev_phase: str | None = None  # 放行後要回到哪個 phase
    human_feedback: str | None = None    # reject 的理由,下一輪當 review 意見用
    # 人已放行過 merge。沒有這個旗標,approve 之後 merge gate 會再次觸發,
    # 變成 park → approve → park 的無限迴圈。
    merge_approved: bool = False

    @property
    def cost_usd(self) -> float:
        return self.implementer_cost_usd + self.reviewer_cost_usd


@dataclass
class PipelineState:
    current: int = 1                                    # 1-based milestone index
    milestones: dict[str, MilestoneState] = field(default_factory=dict)

    def ms(self, index: int) -> MilestoneState:
        key = str(index)
        if key not in self.milestones:
            self.milestones[key] = MilestoneState()
        return self.milestones[key]

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "PipelineState":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        st = cls(current=raw.get("current", 1))
        known = {f.name for f in fields(MilestoneState)}
        for k, v in raw.get("milestones", {}).items():
            # 舊版存檔可能有已移除的欄位(例如合併前的 cost_usd),
            # 直接 **v 會 TypeError,所以濾掉不認得的 key。
            st.milestones[k] = MilestoneState(
                **{kk: vv for kk, vv in v.items() if kk in known}
            )
        return st

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

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

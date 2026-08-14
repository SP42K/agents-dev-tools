"""解析 plan file:每個 `## ` 標題是一個 milestone。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 保留 ASCII 英數與 CJK,其餘(標點、emoji)去掉。
# 中文標題不會整段被清空成空字串,branch 名字才看得懂。
_SLUG_DROP = re.compile(r"[^a-z0-9㐀-䶿一-鿿\s-]")
_SLUG_SEP = re.compile(r"[\s_]+")
_MILESTONE_PREFIX = re.compile(r"^milestone\s*\d+\s*[:：]\s*", re.IGNORECASE)


@dataclass
class Milestone:
    index: int          # 1-based
    title: str
    body: str           # 該 milestone 段落內文

    @property
    def slug(self) -> str:
        s = _SLUG_DROP.sub("", self.title.lower())
        s = _SLUG_SEP.sub("-", s).strip("-")
        return s[:40].strip("-") or f"milestone-{self.index}"

    @property
    def branch(self) -> str:
        return f"milestone/{self.index}-{self.slug}"


@dataclass
class Plan:
    preamble: str       # 第一個 milestone 之前的整體說明
    milestones: list[Milestone]
    raw: str

    @classmethod
    def load(cls, path: Path) -> "Plan":
        raw = path.read_text(encoding="utf-8")
        parts = re.split(r"^##\s+", raw, flags=re.MULTILINE)
        preamble = parts[0].strip()
        milestones = []
        for chunk in parts[1:]:
            lines = chunk.splitlines()
            # `## ` 後面沒東西(例如檔案就以此結尾)時跳過,不要 IndexError
            heading = lines[0].strip() if lines else ""
            if not heading:
                continue
            title = _MILESTONE_PREFIX.sub("", heading) or heading
            body = "\n".join(lines[1:]).strip()
            milestones.append(
                Milestone(index=len(milestones) + 1, title=title, body=body)
            )
        if not milestones:
            raise SystemExit(f"plan file 裡找不到任何 `## ` milestone 標題: {path}")
        return cls(preamble=preamble, milestones=milestones, raw=raw)

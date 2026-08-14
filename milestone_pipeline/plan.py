"""解析 plan file:每個 `## ` 標題是一個 milestone。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Milestone:
    index: int          # 1-based
    title: str
    body: str           # 該 milestone 段落內文

    @property
    def slug(self) -> str:
        s = re.sub(r"[^a-z0-9\s-]", "", self.title.lower())
        s = re.sub(r"[\s_]+", "-", s).strip("-")
        return s[:40] or f"milestone-{self.index}"

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
        for i, chunk in enumerate(parts[1:], start=1):
            lines = chunk.splitlines()
            title = re.sub(r"^milestone\s*\d+\s*[::]\s*", "", lines[0].strip(),
                           flags=re.IGNORECASE) or lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            milestones.append(Milestone(index=i, title=title, body=body))
        if not milestones:
            raise SystemExit(f"plan file 裡找不到任何 `## ` milestone 標題: {path}")
        return cls(preamble=preamble, milestones=milestones, raw=raw)

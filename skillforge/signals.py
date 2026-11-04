"""H14：technical 断言失败的累积告警。

单次 technical 失败**不判死**（它只是环境探针，这是对的）。
但如果同一个探针连续 N 次都响，说明环境真的变了 ——
这时候必须有东西把它升格成「要人看一眼」，否则就是静默退化。

设计取舍：
  - 计数落盘（`<store>/<skill_id>/signals.json`），进程重启不丢。
  - 成功一次就清零，并清掉升格标记（下次复发会重新升格）。
  - **只升格、不自动改技能**。人是最后一道闸，这里只负责喊。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema.models import Skill

DEFAULT_THRESHOLD = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SignalRecord:
    assertion_id: str
    consecutive: int = 0
    total: int = 0
    first_seen: str = ""
    last_seen: str = ""
    last_detail: str = ""
    escalated_at: str = ""

    @property
    def escalated(self) -> bool:
        return bool(self.escalated_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "consecutive": self.consecutive,
            "total": self.total,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_detail": self.last_detail,
            "escalated_at": self.escalated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignalRecord":
        return cls(
            assertion_id=str(data.get("assertion_id") or ""),
            consecutive=int(data.get("consecutive") or 0),
            total=int(data.get("total") or 0),
            first_seen=str(data.get("first_seen") or ""),
            last_seen=str(data.get("last_seen") or ""),
            last_detail=str(data.get("last_detail") or ""),
            escalated_at=str(data.get("escalated_at") or ""),
        )


class SignalBook:
    """按 skill 记录 technical 断言的连续失败次数。"""

    def __init__(self, store_root: Path | str, *, threshold: int = DEFAULT_THRESHOLD) -> None:
        self.root = Path(store_root)
        self.threshold = int(threshold)

    # -- 路径 ---------------------------------------------------------------
    def _path(self, skill_id: str) -> Path:
        if not skill_id or "/" in skill_id or "\\" in skill_id:
            raise ValueError(f"非法 skill_id：{skill_id!r}")
        return self.root / skill_id / "signals.json"

    def _load(self, skill_id: str) -> dict[str, Any]:
        path = self._path(skill_id)
        if not path.exists():
            return {"records": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("records", {})
        return data

    def _save(self, skill_id: str, data: dict[str, Any]) -> None:
        path = self._path(skill_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- 记录 ---------------------------------------------------------------
    @staticmethod
    def _classify(skill: Skill, run: Any) -> tuple[dict[str, str], set[str]]:
        """返回 (失败断言 → 明细, 本轮成功覆盖到的断言 id)。"""
        by_step = {s.step_id: s for s in run.steps}
        failed: dict[str, str] = {}
        passed: set[str] = set()
        for step in skill.steps:
            assertion = step.technical_assertion
            if assertion is None:
                continue
            result = by_step.get(step.step_id)
            if result is None:
                continue
            hits = [f for f in result.technical_failures if f.startswith(assertion.assertion_id)]
            if hits:
                failed[assertion.assertion_id] = hits[0]
            elif result.status in ("ok", "skipped"):
                passed.add(assertion.assertion_id)
        return failed, passed

    def record_run(self, skill: Skill, run: Any) -> list[SignalRecord]:
        """录入一次运行。返回**本次新升格**的信号（同一信号只报一次）。"""
        data = self._load(skill.skill_id)
        records: dict[str, dict[str, Any]] = data["records"]
        failed, passed = self._classify(skill, run)
        newly: list[SignalRecord] = []

        for assertion_id in passed:
            rec = records.get(assertion_id)
            if not rec:
                continue
            rec["consecutive"] = 0
            rec["escalated_at"] = ""  # 恢复了就允许下次复发重新升格

        for assertion_id, detail in failed.items():
            rec = records.get(assertion_id)
            if rec is None:
                rec = {
                    "assertion_id": assertion_id,
                    "consecutive": 0,
                    "total": 0,
                    "first_seen": _now(),
                    "last_seen": "",
                    "last_detail": "",
                    "escalated_at": "",
                }
                records[assertion_id] = rec
            if rec["consecutive"] == 0:
                rec["first_seen"] = _now()
            rec["consecutive"] += 1
            rec["total"] += 1
            rec["last_seen"] = _now()
            rec["last_detail"] = detail
            if rec["consecutive"] >= self.threshold and not rec["escalated_at"]:
                rec["escalated_at"] = _now()
                newly.append(SignalRecord.from_dict(rec))

        self._save(skill.skill_id, data)
        return newly

    # -- 查询 ---------------------------------------------------------------
    def all_records(self, skill_id: str) -> list[SignalRecord]:
        data = self._load(skill_id)
        return sorted(
            (SignalRecord.from_dict(r) for r in data["records"].values()),
            key=lambda r: (-r.consecutive, r.assertion_id),
        )

    def report(self, skill_id: str) -> dict[str, Any]:
        records = self.all_records(skill_id)
        return {
            "skill_id": skill_id,
            "threshold": self.threshold,
            "escalated": [r.to_dict() for r in records if r.escalated],
            "watching": [r.to_dict() for r in records if not r.escalated and r.consecutive],
            "recovered": [r.assertion_id for r in records if not r.consecutive],
        }

    def reset(self, skill_id: str, assertion_id: str | None = None) -> int:
        """人工确认后清零。返回清了几个。"""
        data = self._load(skill_id)
        records = data["records"]
        if assertion_id is None:
            count = len(records)
            records.clear()
        else:
            count = 1 if assertion_id in records else 0
            records.pop(assertion_id, None)
        self._save(skill_id, data)
        return count


__all__ = ["DEFAULT_THRESHOLD", "SignalBook", "SignalRecord"]

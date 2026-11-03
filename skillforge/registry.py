"""版本库：Golden Set 存档 + 过闸发布 + 回滚。

这是闭环最后缺的牙齿。之前「过闸」只是打印一行日志：
没有任何东西把新版本记下来，也没有一条命令能退回上一版。

设计原则：
  - **没过闸 = 不入库**。拒绝路径抛 GateRejected，不产生任何文件改动。
  - **Golden Set 只增不删**：`add_case` 只追加，没有删除接口。
  - **回滚是移动指针**，不删历史版本，所以可以来回切。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .schema.models import CaseScore, GateResult, GoldenCase, Skill, monotonic_gate
from .schema.serde import golden_case_from_dict, skill_from_dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def version_key(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version or "")
    nums = tuple(int(p) for p in parts[:3])
    return nums + (0,) * (3 - len(nums))


def bump_minor(version: str) -> str:
    major, minor, _patch = version_key(version)
    return f"{major}.{minor + 1}.0"


class GateRejected(Exception):
    """单调性闸拒绝。**库不会发生任何改动**。"""

    def __init__(self, gate: GateResult) -> None:
        self.gate = gate
        super().__init__("单调性闸拒绝：" + "；".join(gate.reasons))


@dataclass
class ReleaseRecord:
    version: str
    created_at: str = ""
    rate: float = 0.0
    gate: dict[str, Any] = field(default_factory=dict)
    cases: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "rate": round(self.rate, 4),
            "gate": dict(self.gate),
            "cases": [dict(c) for c in self.cases],
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReleaseRecord":
        return cls(
            version=str(data.get("version") or "0.0.0"),
            created_at=str(data.get("created_at") or ""),
            rate=float(data.get("rate") or 0.0),
            gate=dict(data.get("gate") or {}),
            cases=[dict(c) for c in (data.get("cases") or [])],
            note=str(data.get("note") or ""),
        )


class SkillStore:
    """一个技能一个目录：

        <root>/<skill_id>/store.json        当前版本 + 全部发布记录
        <root>/<skill_id>/golden.json       Golden Set（只增不删）
        <root>/<skill_id>/versions/<v>.json 每个版本的完整 Skill
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- 路径 ---------------------------------------------------------------
    def skill_dir(self, skill_id: str) -> Path:
        if not skill_id or "/" in skill_id or "\\" in skill_id:
            raise ValueError(f"非法 skill_id：{skill_id!r}")
        return self.root / skill_id

    def _store_path(self, skill_id: str) -> Path:
        return self.skill_dir(skill_id) / "store.json"

    def _golden_path(self, skill_id: str) -> Path:
        return self.skill_dir(skill_id) / "golden.json"

    def _version_path(self, skill_id: str, version: str) -> Path:
        return self.skill_dir(skill_id) / "versions" / f"{version}.json"

    # -- 低层读写 -----------------------------------------------------------
    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load_store(self, skill_id: str) -> dict[str, Any]:
        data = self._read_json(self._store_path(skill_id), {})
        data.setdefault("current", None)
        data.setdefault("records", [])
        return data

    # -- Golden Set ---------------------------------------------------------
    def cases(self, skill_id: str) -> list[GoldenCase]:
        raw = self._read_json(self._golden_path(skill_id), [])
        return [golden_case_from_dict(x) for x in raw]

    def save_cases(self, skill_id: str, cases: Sequence[GoldenCase]) -> Path:
        path = self._golden_path(skill_id)
        self._write_json(path, [c.to_dict() for c in cases])
        return path

    def add_case(self, skill_id: str, case: GoldenCase) -> list[GoldenCase]:
        """只增不删：同 case_id 已存在则报错，避免静默覆盖历史判据。"""
        existing = self.cases(skill_id)
        if any(c.case_id == case.case_id for c in existing):
            raise ValueError(f"Golden 用例重复：{case.case_id}（只增不删，换一个 case_id）")
        existing.append(case)
        self.save_cases(skill_id, existing)
        return existing

    # -- 发布 ---------------------------------------------------------------
    @staticmethod
    def _scores_of(record_cases: Sequence[dict[str, Any]]) -> list[CaseScore]:
        return [
            CaseScore(
                case_id=str(c.get("case_id") or ""),
                passed=bool(c.get("passed")),
                detail=str(c.get("detail") or ""),
            )
            for c in record_cases
        ]

    @staticmethod
    def _first_release_gate(scores: Sequence[CaseScore]) -> GateResult:
        total = len(scores)
        passed = sum(1 for s in scores if s.passed)
        rate = passed / total if total else 0.0
        reasons: list[str] = []
        if total == 0:
            reasons.append("Golden Set 为空：没有判据不准发布。")
        else:
            failed = [s.case_id for s in scores if not s.passed]
            if failed:
                reasons.append(f"首版必须让 Golden Set 全过，未过：{failed}")
        return GateResult(passed=not reasons, reasons=reasons, old_rate=0.0, new_rate=rate)

    def release(
        self,
        skill: Skill,
        scores: Sequence[CaseScore],
        *,
        note: str = "",
    ) -> tuple[ReleaseRecord, GateResult]:
        """过闸才入库。没过闸抛 GateRejected，且**不写任何文件**。"""
        scores = list(scores)
        data = self._load_store(skill.skill_id)
        records = data["records"]
        current = data.get("current")

        if current:
            prev = next((r for r in records if r.get("version") == current), None)
            if prev is None:
                raise ValueError(f"store.json 里 current={current} 但找不到对应记录")
            old = self._scores_of(prev.get("cases") or [])
            gate = monotonic_gate(old, scores)
        else:
            gate = self._first_release_gate(scores)

        if not gate.passed:
            raise GateRejected(gate)

        existing = {str(r.get("version")) for r in records}
        version = skill.version
        if version in existing:
            latest = max(existing, key=version_key)
            version = bump_minor(latest)

        skill.version = version
        self._write_json(self._version_path(skill.skill_id, version), {"skill": skill.to_dict()})

        record = ReleaseRecord(
            version=version,
            created_at=_now(),
            rate=gate.new_rate,
            gate={"old_rate": round(gate.old_rate, 4), "new_rate": round(gate.new_rate, 4)},
            cases=[
                {"case_id": s.case_id, "passed": s.passed, "detail": s.detail} for s in scores
            ],
            note=note,
        )
        records.append(record.to_dict())
        data["records"] = records
        data["current"] = version
        self._write_json(self._store_path(skill.skill_id), data)
        return record, gate

    # -- 查询 / 回滚 --------------------------------------------------------
    def history(self, skill_id: str) -> list[ReleaseRecord]:
        data = self._load_store(skill_id)
        return [ReleaseRecord.from_dict(r) for r in data["records"]]

    def current_record(self, skill_id: str) -> ReleaseRecord | None:
        data = self._load_store(skill_id)
        cur = data.get("current")
        if not cur:
            return None
        for r in data["records"]:
            if r.get("version") == cur:
                return ReleaseRecord.from_dict(r)
        return None

    def load_skill(self, skill_id: str, version: str | None = None) -> Skill | None:
        """读某个版本的 Skill；version 省略则读当前版本。"""
        if version is None:
            record = self.current_record(skill_id)
            if record is None:
                return None
            version = record.version
        path = self._version_path(skill_id, version)
        if not path.exists():
            return None
        payload = self._read_json(path, {})
        return skill_from_dict(payload.get("skill") or {})

    def rollback(self, skill_id: str, to: str | None = None) -> ReleaseRecord:
        """回滚 = 移动 current 指针，不删任何历史版本。"""
        data = self._load_store(skill_id)
        records = data["records"]
        if not records:
            raise ValueError(f"{skill_id} 还没有任何发布版本")
        current = data.get("current")
        versions = [str(r.get("version")) for r in records]

        if to is None:
            if current is None or versions.index(current) == 0:
                raise ValueError("已经是最早的版本，无法再回滚")
            to = versions[versions.index(current) - 1]
        if to not in versions:
            raise ValueError(f"版本不存在：{to}（可用：{versions}）")

        data["current"] = to
        self._write_json(self._store_path(skill_id), data)
        return ReleaseRecord.from_dict(next(r for r in records if r.get("version") == to))

    def status(self, skill_id: str) -> dict[str, Any]:
        record = self.current_record(skill_id)
        return {
            "skill_id": skill_id,
            "current": record.version if record else None,
            "rate": record.rate if record else 0.0,
            "golden_cases": len(self.cases(skill_id)),
            "releases": [r.version for r in self.history(skill_id)],
        }


def save_skill(skill: Skill, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(skill.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_skill(path: Path | str) -> Skill:
    return skill_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


__all__ = [
    "GateRejected",
    "ReleaseRecord",
    "SkillStore",
    "bump_minor",
    "load_skill",
    "save_skill",
    "version_key",
]

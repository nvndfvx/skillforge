"""灰度发布：让新版本先接一小部分真实请求，样本够了再全量。

三个不变量，破坏了灰度就没有意义：

1. **按 key 粘性路由** —— 同一个 item 永远走同一个版本。
   用随机数会让同一张单被两个版本各提交一次；必须用 `hash(skill_id::item_id)`。
   而且哈希要跨进程、跨机器稳定，所以不用 Python 内置 `hash()`（它有随机种子）。
2. **灰度不能替代发布闸** —— `promote` 仍要求候选版本过单调性闸（`--scores`）。
   灰度验证的是"真实数据上没更差"，闸验证的是"基准集上没退化"，两件事。
3. **统计按版本分开** —— 候选的失败绝不能算到稳定版头上，否则灰度结论就是错的。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BUCKETS = 10_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bucket(skill_id: str, key: str) -> int:
    """稳定的分桶：0..BUCKETS-1。跨进程/跨机器/跨版本都不变。"""
    digest = hashlib.blake2b(f"{skill_id}::{key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % BUCKETS


@dataclass
class CanaryState:
    skill_id: str
    candidate_version: str
    weight: float = 0.1
    min_samples: int = 20
    created_at: str = ""
    samples: dict[str, dict[str, int]] = dc_field(default_factory=dict)
    # 分流维度：
    #   item    —— 按 item_id 哈希（同一张单永远同一版本）
    #   param   —— 按 params[field] 哈希（**同一个用户**永远同一版本）
    #   segment —— 只把 params[field] 在白名单里的放给候选（100%，不看 weight）
    mode: str = "item"
    field: str = ""
    segments: list[str] = dc_field(default_factory=list)

    # -- 路由 ---------------------------------------------------------------
    def route_key(self, item_id: str, params: dict[str, Any] | None = None) -> str:
        """这一条的分流依据。

        取不到参数时退回 item_id —— **不能崩，也不能退化成"全走稳定版"**，
        否则缺一个字段就悄悄让灰度变成 0% 或 100%。
        """
        if self.mode in ("param", "segment") and self.field:
            value = (params or {}).get(self.field)
            if value is None or value == "":
                return item_id
            return str(value)
        return item_id

    def routes(self, item_id: str, params: dict[str, Any] | None = None) -> bool:
        """这一条该不该走候选版本。同一个分流依据永远给同一个答案。"""
        key = self.route_key(item_id, params)
        if self.mode == "segment":
            return key in set(self.segments)
        threshold = round(self.weight * BUCKETS)
        return bucket(self.skill_id, key) < threshold

    def actual_weight(self, keys: list[str]) -> float:
        """给定分流依据集合的真实比例（可能偏离 weight，样本小的时候很正常）。"""
        if not keys:
            return 0.0
        if self.mode == "segment":
            return round(sum(1 for k in keys if k in set(self.segments)) / len(keys), 4)
        return round(sum(1 for k in keys if self.routes(k)) / len(keys), 4)

    def describe(self) -> str:
        if self.mode == "param":
            return f"按参数「{self.field}」分桶（同一用户永远同一版本）"
        if self.mode == "segment":
            return f"只放行「{self.field}」属于 {self.segments} 的请求（100%）"
        return "按 item_id 分桶（同一张单永远同一版本）"

    # -- 统计 ---------------------------------------------------------------
    def record(self, version: str, ok: bool) -> None:
        slot = self.samples.setdefault(version, {"ok": 0, "fail": 0})
        slot["ok" if ok else "fail"] += 1

    def _rate(self, version: str) -> tuple[int, float]:
        slot = self.samples.get(version) or {"ok": 0, "fail": 0}
        total = slot["ok"] + slot["fail"]
        return total, (round(slot["ok"] / total, 4) if total else 0.0)

    def verdict(self, stable_version: str) -> dict[str, Any]:
        """够不够样本？候选是不是更差？"""
        stable_total, stable_rate = self._rate(stable_version)
        cand_total, cand_rate = self._rate(self.candidate_version)
        enough = cand_total >= self.min_samples
        worse = cand_total > 0 and cand_rate < stable_rate

        if worse and cand_total >= max(1, min(self.min_samples, 5)):
            decision, reason = "abort", (
                f"候选通过率 {cand_rate:.2%} 低于稳定版 {stable_rate:.2%}"
            )
        elif not enough:
            decision, reason = "wait", (
                f"候选样本 {cand_total}/{self.min_samples}，还看不出结论"
            )
        elif cand_rate >= stable_rate:
            decision, reason = "promote", (
                f"候选 {cand_rate:.2%} >= 稳定版 {stable_rate:.2%}，样本 {cand_total} 条"
            )
        else:
            decision, reason = "abort", "候选不如稳定版"

        return {
            "decision": decision,
            "reason": reason,
            "weight": self.weight,
            "min_samples": self.min_samples,
            "candidate_version": self.candidate_version,
            "mode": self.mode,
            "field": self.field,
            "segments": list(self.segments),
            "routing": self.describe(),
            "candidate": {"samples": cand_total, "rate": cand_rate},
            "stable": {"version": stable_version, "samples": stable_total, "rate": stable_rate},
            "enough_samples": enough,
        }

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "candidate_version": self.candidate_version,
            "weight": self.weight,
            "min_samples": self.min_samples,
            "created_at": self.created_at or _now(),
            "samples": self.samples,
            "mode": self.mode,
            "field": self.field,
            "segments": list(self.segments),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanaryState":
        return cls(
            skill_id=str(data.get("skill_id") or ""),
            candidate_version=str(data.get("candidate_version") or ""),
            weight=float(data.get("weight") or 0.0),
            min_samples=int(data.get("min_samples") or 20),
            created_at=str(data.get("created_at") or ""),
            samples={
                str(k): {"ok": int(v.get("ok") or 0), "fail": int(v.get("fail") or 0)}
                for k, v in (data.get("samples") or {}).items()
            },
            mode=str(data.get("mode") or "item"),
            field=str(data.get("field") or ""),
            segments=[str(s) for s in (data.get("segments") or [])],
        )


class CanaryBook:
    """灰度状态落盘在 `<store>/<skill_id>/canary.json`。"""

    def __init__(self, store_root: Path | str) -> None:
        self.root = Path(store_root)

    def path(self, skill_id: str) -> Path:
        if not skill_id or "/" in skill_id or "\\" in skill_id:
            raise ValueError(f"非法 skill_id：{skill_id!r}")
        return self.root / skill_id / "canary.json"

    def candidate_path(self, skill_id: str) -> Path:
        return self.root / skill_id / "canary-skill.json"

    def load(self, skill_id: str) -> CanaryState | None:
        path = self.path(skill_id)
        if not path.exists():
            return None
        return CanaryState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, state: CanaryState) -> None:
        path = self.path(state.skill_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def clear(self, skill_id: str) -> bool:
        removed = False
        for path in (self.path(skill_id), self.candidate_path(skill_id)):
            if path.exists():
                path.unlink()
                removed = True
        return removed


__all__ = ["BUCKETS", "CanaryBook", "CanaryState", "bucket"]

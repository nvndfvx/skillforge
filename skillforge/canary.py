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
from dataclasses import dataclass, field
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
    samples: dict[str, dict[str, int]] = field(default_factory=dict)

    # -- 路由 ---------------------------------------------------------------
    def routes(self, key: str) -> bool:
        """这个 key 该不该走候选版本。同一个 key 永远给同一个答案。"""
        threshold = round(self.weight * BUCKETS)
        return bucket(self.skill_id, key) < threshold

    def actual_weight(self, keys: list[str]) -> float:
        """给定 key 集合的真实分流比例（可能偏离 weight，样本小的时候很正常）。"""
        if not keys:
            return 0.0
        return round(sum(1 for k in keys if self.routes(k)) / len(keys), 4)

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

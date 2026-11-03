"""幂等门：写操作能不能跑，由这里说了算。

核心原则（v3 §3 地基④）：
  - 没有 precheck 的写操作 → **拒绝执行**，不是"放行然后赌"。
  - precheck 命中（记录已存在）→ 跳过，不再提交。
  - precheck 未命中 → 执行。

默认方向在这里最要命：宁可少跑一次，也不要重复提交一次。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..schema.models import Assertion, Step
from ..schema.types import WriteEffect
from .adapter import PageAdapter
from .validator import AssertionResult, evaluate

PROCEED = "proceed"
SKIP = "skip"
BLOCKED = "blocked"


@dataclass
class IdempotencyDecision:
    decision: str
    reason: str
    precheck: AssertionResult | None = None

    @property
    def is_blocked(self) -> bool:
        return self.decision == BLOCKED

    @property
    def is_skip(self) -> bool:
        return self.decision == SKIP

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "precheck": self.precheck.to_dict() if self.precheck else None,
        }


async def decide(step: Step, page: PageAdapter) -> IdempotencyDecision:
    if not step.is_write:
        return IdempotencyDecision(PROCEED, "非写操作，无需查重")

    if step.precheck is None:
        return IdempotencyDecision(
            BLOCKED,
            f"写操作（{step.write_effect.value}）缺 precheck：不知道要查什么，"
            "为防止重复提交，拒绝执行。请人审时补上「怎么判断这张单已存在」。",
        )

    result = await evaluate(step.precheck, page)
    if result.unsupported:
        return IdempotencyDecision(
            BLOCKED, f"precheck 无法求值（{result.detail}）：按保守策略拒绝执行。", result
        )
    if result.passed:
        return IdempotencyDecision(
            SKIP, f"precheck 命中（{result.detail}）：记录已存在，跳过写操作。", result
        )
    return IdempotencyDecision(
        PROCEED, f"precheck 未命中（{result.detail}）：记录不存在，可以执行。", result
    )


def requires_precheck(step: Step) -> bool:
    """写副作用不是 none 一律要求 precheck（unknown 也算写）。"""
    return step.write_effect is not WriteEffect.NONE


__all__ = [
    "BLOCKED",
    "PROCEED",
    "SKIP",
    "IdempotencyDecision",
    "decide",
    "requires_precheck",
]

"""Healer 自动打补丁：改一版 → 跑闸 → 被拒 / 被收。

这是闭环最后一段自动化。三条硬规则：

1. **只在封闭算子集里搜**（见 triage.OPERATORS），且只应用 `auto_applicable=True` 的。
2. **一次只改一个算子**，改完立刻跑 Golden Set 过闸。
3. **没过闸 = 什么都没发生**。被拒时不会留下任何版本，换下一个算子继续试。

`swap_locator` 需要「新定位器候选」，必须由调用方从失败页面收集
（`executor.candidates.collect_locator_candidates`）——补丁不能凭空造定位器。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..registry import GateRejected, SkillStore
from ..schema.models import CaseScore, GateResult, Skill, Step, monotonic_gate
from ..schema.types import Locator
from .triage import Triage, triage

RunGolden = Callable[[Skill], Awaitable[list[CaseScore]]]

MAX_TIMEOUT_MS = 30_000
TIMEOUT_MULTIPLIER = 3


def _step_by_id(skill: Skill, step_id: int) -> Step | None:
    for step in skill.steps:
        if step.step_id == step_id:
            return step
    return None


# --------------------------------------------------------------------------- #
# 算子实现：返回 (改好的副本 | None, 说明)
# --------------------------------------------------------------------------- #
def _op_swap_locator(skill: Skill, ctx: "HealContext") -> tuple[Skill | None, str]:
    step = _step_by_id(skill, ctx.failed_step.step_id)
    if step is None:
        return None, "找不到失败步骤"
    current = step.target.key() if step.target else "<空>"
    fresh = [
        c
        for c in ctx.locator_candidates.get(step.step_id, [])
        if c.key() != current
    ]
    if not fresh:
        return None, "没有可用的新定位器候选（需要从失败页面收集）"

    patched = copy.deepcopy(skill)
    target_step = _step_by_id(patched, ctx.failed_step.step_id)
    if target_step is None:
        return None, "深拷贝后找不到失败步骤"
    target_step.target = fresh[0]
    return patched, f"定位器 {current} → {fresh[0].key()}"


def _op_add_wait(skill: Skill, ctx: "HealContext") -> tuple[Skill | None, str]:
    patched = copy.deepcopy(skill)
    step = _step_by_id(patched, ctx.failed_step.step_id)
    if step is None:
        return None, "找不到失败步骤"
    changed: list[str] = []
    for assertion in (step.assertion, step.technical_assertion):
        if assertion is None:
            continue
        if assertion.timeout_ms >= MAX_TIMEOUT_MS:
            continue
        assertion.timeout_ms = min(MAX_TIMEOUT_MS, assertion.timeout_ms * TIMEOUT_MULTIPLIER)
        changed.append(f"{assertion.assertion_id} timeout→{assertion.timeout_ms}ms")
    if not changed:
        return None, "该步没有可延长的断言（或已到上限）"
    return patched, "；".join(changed)


def _op_drop_optional_step(skill: Skill, ctx: "HealContext") -> tuple[Skill | None, str]:
    step = _step_by_id(skill, ctx.failed_step.step_id)
    if step is None:
        return None, "找不到失败步骤"
    if step.is_write:
        return None, "失败步是写操作，不允许自动删除"
    if skill.steps and skill.steps[-1].step_id == step.step_id:
        return None, "失败步带着业务断言，不允许自动删除"
    if step.assertion is not None and step.assertion.kind.value == "business":
        return None, "失败步挂着 business 断言，不允许自动删除"

    patched = copy.deepcopy(skill)
    patched.steps = [s for s in patched.steps if s.step_id != step.step_id]
    for index, s in enumerate(patched.steps, start=1):
        s.step_id = index
    return patched, f"删除 step {step.step_id}"


_APPLIERS: dict[str, Callable[[Skill, "HealContext"], tuple[Skill | None, str]]] = {
    "swap_locator": _op_swap_locator,
    "add_wait": _op_add_wait,
    "drop_optional_step": _op_drop_optional_step,
}


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass
class HealContext:
    failed_step: Any
    locator_candidates: dict[int, list[Locator]] = field(default_factory=dict)


@dataclass
class PatchAttempt:
    operator_id: str
    applied: bool
    accepted: bool = False
    detail: str = ""
    reason: str = ""
    scores: list[CaseScore] = field(default_factory=list)
    gate: GateResult | None = None

    @property
    def rate(self) -> float:
        if not self.scores:
            return 0.0
        return sum(1 for s in self.scores if s.passed) / len(self.scores)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "applied": self.applied,
            "accepted": self.accepted,
            "detail": self.detail,
            "reason": self.reason,
            "rate": round(self.rate, 4),
            "cases": len(self.scores),
            "failed_cases": [s.case_id for s in self.scores if not s.passed],
            "gate": self.gate.to_dict() if self.gate else None,
        }


@dataclass
class HealResult:
    accepted: bool = False
    skill: Skill | None = None
    version: str | None = None
    attempts: list[PatchAttempt] = field(default_factory=list)
    needs_human: bool = False
    layer: str = "none"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "version": self.version,
            "layer": self.layer,
            "needs_human": self.needs_human,
            "reasons": list(self.reasons),
            "attempts": [a.to_dict() for a in self.attempts],
        }


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
async def heal(
    skill: Skill,
    failed_run: Any,
    *,
    run_golden: RunGolden,
    store: SkillStore | None = None,
    baseline_scores: list[CaseScore] | None = None,
    locator_candidates: dict[int, list[Locator]] | None = None,
    max_attempts: int = 3,
    note: str = "",
) -> HealResult:
    """三层定位 → 逐一试封闭算子 → 每个候选都要过闸。

    store 与 baseline_scores 至少给一个：
      - 给 store：用版本库里的历史记录当基线，过闸即入库（真发布路径）
      - 只给 baseline_scores：只试补丁不发布（干跑）
    """
    if store is None and baseline_scores is None:
        raise ValueError("heal 需要 store 或 baseline_scores 之一，否则无法过闸")

    verdict: Triage = triage(failed_run)
    result = HealResult(
        layer=verdict.layer,
        needs_human=verdict.needs_human,
        reasons=list(verdict.reasons),
    )
    if verdict.needs_human:
        result.reasons.append("triage 判断需要人工：没有可自动应用的算子")
        return result

    failed_step = failed_run.failed_step()
    if failed_step is None:
        result.reasons.append("运行结果里没有失败步骤")
        result.needs_human = True
        return result

    ctx = HealContext(failed_step=failed_step, locator_candidates=locator_candidates or {})
    tried = 0

    for op in verdict.operators:
        if tried >= max_attempts:
            result.reasons.append(f"已达最大尝试次数 {max_attempts}")
            break
        if not op.auto_applicable:
            result.attempts.append(
                PatchAttempt(op.operator_id, applied=False, reason="该算子不允许自动应用")
            )
            continue
        applier = _APPLIERS.get(op.operator_id)
        if applier is None:
            result.attempts.append(
                PatchAttempt(op.operator_id, applied=False, reason="算子尚未实现")
            )
            continue

        patched, detail = applier(skill, ctx)
        attempt = PatchAttempt(op.operator_id, applied=patched is not None, detail=detail)
        if patched is None:
            result.attempts.append(attempt)
            continue

        tried += 1
        patched.meta["patched_by"] = op.operator_id
        patched.meta["patch_detail"] = detail
        scores = await run_golden(patched)
        attempt.scores = scores

        try:
            if store is not None:
                record, gate = store.release(
                    patched, scores, note=note or f"auto-heal: {op.operator_id}"
                )
                attempt.accepted = True
                attempt.gate = gate
                result.accepted = True
                result.skill = patched
                result.version = record.version
            else:
                gate = monotonic_gate(list(baseline_scores or []), scores)
                attempt.gate = gate
                if gate.passed:
                    attempt.accepted = True
                    result.accepted = True
                    result.skill = patched
                    result.version = patched.version
        except GateRejected as exc:
            attempt.gate = exc.gate
            attempt.reason = "；".join(exc.gate.reasons)

        result.attempts.append(attempt)
        if result.accepted:
            return result

    result.needs_human = True
    if not result.accepted:
        result.reasons.append(
            f"{tried} 个候选算子都没过闸，需人工介入"
            if tried
            else "没有可应用的候选算子，需人工介入"
        )
    return result


__all__ = [
    "HealContext",
    "HealResult",
    "MAX_TIMEOUT_MS",
    "PatchAttempt",
    "RunGolden",
    "heal",
]

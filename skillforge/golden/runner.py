"""Golden Set 跑批 + 单调性闸接线。

「越用越准」的证据链：跑批 → 出 CaseScore → 过单调性闸 → 才允许发新版。
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Sequence

from ..executor.adapter import PageAdapter
from ..executor.driver import RunResult, execute
from ..schema.models import CaseScore, GateResult, GoldenCase, Skill, monotonic_gate

PageFactory = Callable[[GoldenCase], "PageAdapter | Awaitable[PageAdapter]"]


async def run_golden(
    skill: Skill,
    cases: Sequence[GoldenCase],
    page_factory: PageFactory,
) -> list[CaseScore]:
    """在 Golden Set 上跑一遍，产出每条用例的通过与失败。"""
    scores: list[CaseScore] = []
    for case in cases:
        page = page_factory(case)
        if inspect.isawaitable(page):
            page = await page
        run: RunResult = await execute(
            skill, case.params, page, run_id=f"golden::{case.case_id}"
        )
        failed = run.failed_step()
        scores.append(
            CaseScore(
                case_id=case.case_id,
                passed=run.ok,
                detail=(failed.detail or failed.status) if failed else "",
            )
        )
    return scores


def evaluate_gate(old: Sequence[CaseScore], new: Sequence[CaseScore]) -> GateResult:
    """单调性闸：新版本不得在 Golden Set 上退化。"""
    return monotonic_gate(list(old), list(new))


def summarize(scores: Sequence[CaseScore]) -> dict[str, Any]:
    total = len(scores)
    passed = sum(1 for s in scores if s.passed)
    return {
        "total": total,
        "passed": passed,
        "rate": round(passed / total, 4) if total else 0.0,
        "failed_cases": [s.case_id for s in scores if not s.passed],
    }


__all__ = ["PageFactory", "evaluate_gate", "run_golden", "summarize"]

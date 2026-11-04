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
# CLI 域用：直接给一个「跑一次技能」的函数，就不需要页面了
SkillRunner = Callable[[Skill, dict[str, Any]], Awaitable[RunResult]]


async def run_golden(
    skill: Skill,
    cases: Sequence[GoldenCase],
    page_factory: PageFactory | None = None,
    *,
    runner: SkillRunner | None = None,
) -> list[CaseScore]:
    """在 Golden Set 上跑一遍，产出每条用例的通过与失败。

    两种跑法二选一：
      - `page_factory`：浏览器域，每条用例给一个页面
      - `runner`：CLI 域（或任何自带执行器的域），直接跑
    这样「基准集 + 单调性闸」不必区分域 —— 凡是能分层失败的执行，都能被闸住。
    """
    if page_factory is None and runner is None:
        raise ValueError("run_golden 需要 page_factory（浏览器域）或 runner（CLI 域）之一")

    scores: list[CaseScore] = []
    for case in cases:
        if runner is not None:
            run: RunResult = await runner(skill, dict(case.params))
        else:
            page = page_factory(case)  # type: ignore[misc]
            if inspect.isawaitable(page):
                page = await page
            run = await execute(
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


__all__ = ["PageFactory", "SkillRunner", "evaluate_gate", "run_golden", "summarize"]

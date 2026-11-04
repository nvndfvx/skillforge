"""从真实页面收集「新定位器候选」，给 swap_locator 用。

补丁不能凭空造定位器 —— 必须从**失败时的页面**上，按元素指纹重新认一遍。
"""
from __future__ import annotations

from typing import Sequence

from ..schema.models import Skill
from ..schema.types import ElementFingerprint, Locator
from .adapter import PageAdapter

MIN_CANDIDATE_SCORE = 0.5


async def collect_locator_candidates(
    page: PageAdapter,
    skill: Skill,
    *,
    min_score: float = MIN_CANDIDATE_SCORE,
    limit: int = 3,
) -> dict[int, list[Locator]]:
    """给每个有指纹的步骤，按相似度排出一串候选定位器。

    只给「指纹还认得出」的步骤 —— 指纹都认不出时说明变化太大，该走人工。
    """
    fingerprints: Sequence[ElementFingerprint] = await page.fingerprints()
    out: dict[int, list[Locator]] = {}
    for step in skill.steps:
        anchor = step.fingerprint
        if anchor is None:
            continue
        ranked = sorted(
            ((anchor.similarity(fp), fp) for fp in fingerprints),
            key=lambda pair: pair[0],
            reverse=True,
        )
        candidates: list[Locator] = []
        for score, fp in ranked:
            if score < min_score or len(candidates) >= limit:
                break
            locator = Locator(role=fp.role or None, name=fp.accessible_name or None)
            if locator.is_empty():
                continue
            if any(locator.key() == c.key() for c in candidates):
                continue
            candidates.append(locator)
        if candidates:
            out[step.step_id] = candidates
    return out


__all__ = ["MIN_CANDIDATE_SCORE", "collect_locator_candidates"]

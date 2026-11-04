"""执行器：把 Skill 跑在 PageAdapter 上，逐步校验断言与幂等。

失败分层写在 StepResult.failure_layer 里，Healer 直接拿它做三层定位：
  param     参数缺失/类型错
  locator   元素找不到（多半是环境变了）
  assertion 动作跑完了但业务判据没过
  precheck  写操作缺 precheck，被拒绝执行
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from ..schema.models import Skill, Step
from ..schema.types import ActionKind, Locator, WriteEffect
from .adapter import PageAdapter
from .idempotency import BLOCKED, SKIP, decide
from .validator import evaluate

MIN_FP_MATCH = 0.75
_TEMPLATE = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass
class StepResult:
    step_id: int
    action: str
    status: str = "ok"  # ok | failed | skipped | blocked
    detail: str = ""
    failure_layer: str = ""  # param | locator | assertion | precheck | ""
    business_failures: list[str] = field(default_factory=list)
    technical_failures: list[str] = field(default_factory=list)
    healed_locator: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "status": self.status,
            "detail": self.detail,
            "failure_layer": self.failure_layer,
            "business_failures": list(self.business_failures),
            "technical_failures": list(self.technical_failures),
            "healed_locator": self.healed_locator,
        }


@dataclass
class RunResult:
    run_id: str
    skill_id: str
    version: str
    status: str  # success | failed
    steps: list[StepResult] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def failed_step(self) -> StepResult | None:
        for s in self.steps:
            if s.status in ("failed", "blocked"):
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "skill_id": self.skill_id,
            "version": self.version,
            "status": self.status,
            "params": dict(self.params),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": [s.to_dict() for s in self.steps],
        }


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def render(value: Any, params: Mapping[str, Any]) -> Any:
    """把 {{参数}} 替换成实际值。缺参数抛 KeyError（→ param 层失败）。"""
    if not isinstance(value, str) or "{{" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise KeyError(key)
        return str(params[key])

    return _TEMPLATE.sub(repl, value)


async def resolve_locator(
    step: Step, page: PageAdapter
) -> tuple[Locator | None, bool, str]:
    """先试原定位器，失败则按元素指纹重匹配（地基② 的运行时价值）。

    返回 (locator, healed, detail)。
    """
    target = step.target
    if target is not None and not target.is_empty():
        if await page.exists(target):
            # 写操作必须唯一命中。真实浏览器上 role+name 是**子串**匹配，
            # 「提交」能匹配到「提交保存」「撤销提交」——写操作点错一个就出事。
            if step.is_write:
                hits = await page.count(target)
                if hits != 1:
                    return None, False, f"写操作定位不唯一（命中 {hits} 个），拒绝点击"
            return target, False, ""

    fp = step.fingerprint
    if fp is not None:
        best = None
        best_score = 0.0
        for cand in await page.fingerprints():
            score = fp.similarity(cand)
            if score > best_score:
                best, best_score = cand, score
        if best is not None and best_score >= MIN_FP_MATCH:
            candidate = Locator(role=best.role or None, name=best.accessible_name or None)
            if await page.exists(candidate):
                return candidate, True, f"定位器失效，已按指纹重匹配（相似度 {best_score:.2f}）"

    return None, False, "locator_not_found"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 单步 / 整体
# --------------------------------------------------------------------------- #
async def run_step(step: Step, params: Mapping[str, Any], page: PageAdapter) -> StepResult:
    result = StepResult(step_id=step.step_id, action=step.action.value)

    # 多标签：这一步要在哪个标签页里做。单标签技能全是 0，等于没这段。
    tab = int(getattr(step, "tab", 0) or 0)
    if tab:
        use_tab = getattr(page, "use_tab", None)
        if use_tab is None:
            result.status = "failed"
            result.failure_layer = "step"
            result.detail = (
                f"step {step.step_id} 要求在第 {tab} 个标签页执行，"
                "但当前页面适配器不支持多标签（请用 launch_tabbed 启动）"
            )
            return result
        try:
            await use_tab(tab)
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.failure_layer = "step"
            result.detail = f"切不到第 {tab} 个标签页：{type(exc).__name__}: {exc}"
            return result


    # 1) 幂等门：写操作没有 precheck 直接拒绝
    decision = await decide(step, page)
    if decision.decision == BLOCKED:
        result.status = "blocked"
        result.failure_layer = "precheck"
        result.detail = decision.reason
        return result
    if decision.decision == SKIP:
        result.status = "skipped"
        result.detail = decision.reason
        return result

    # 2) 参数渲染
    try:
        value = render(step.value, params)
    except KeyError as exc:
        result.status = "failed"
        result.failure_layer = "param"
        result.detail = f"缺少参数 {exc.args[0]!r}"
        return result

    # 3) 执行动作
    try:
        if step.action is ActionKind.GOTO:
            await page.goto(str(value or ""))
        elif step.action in (ActionKind.CLICK, ActionKind.SUBMIT):
            locator, healed, detail = await resolve_locator(step, page)
            if locator is None:
                result.status = "failed"
                result.failure_layer = "locator"
                result.detail = detail
                return result
            result.healed_locator = healed
            result.detail = detail
            await page.click(locator)
        elif step.action in (ActionKind.FILL, ActionKind.SELECT):
            locator, healed, detail = await resolve_locator(step, page)
            if locator is None:
                result.status = "failed"
                result.failure_layer = "locator"
                result.detail = detail
                return result
            result.healed_locator = healed
            result.detail = detail
            await page.fill(locator, str(value if value is not None else ""))
        # READ_ENTITY / WAIT：只读或纯等待，不驱动页面
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.failure_layer = "step"
        result.detail = f"动作执行异常：{type(exc).__name__}: {exc}"
        return result

    # 4) 断言：business 失败判死；technical 失败只记环境信号
    if step.assertion is not None:
        outcome = await evaluate(step.assertion, page)
        if not outcome.passed:
            result.business_failures.append(f"{outcome.assertion_id}: {outcome.detail}")
    if step.technical_assertion is not None:
        outcome = await evaluate(step.technical_assertion, page)
        if not outcome.passed:
            result.technical_failures.append(f"{outcome.assertion_id}: {outcome.detail}")

    # 4.5) 跨页关联：这个标签页里必须真出现你要求的那个业务实体。
    # 少了这一层，点错行/审错单子照样"提交成功"，断言照样通过 —— 稳定地把事做错。
    check = getattr(step, "entity_check", None)
    if check is not None:
        expected = params.get(check.param)
        if expected in (None, ""):
            result.status = "failed"
            result.failure_layer = "param"
            result.detail = f"跨页关联检查缺参数「{check.param}」：无法证明停在了正确的记录上"
            return result
        try:
            page_text = await page.text()
        except Exception:  # noqa: BLE001
            page_text = ""
        try:
            found = set(re.findall(check.pattern, page_text or ""))
        except re.error as exc:
            result.status = "failed"
            result.failure_layer = "assertion"
            result.detail = f"关联性断言的正则写错了：{exc}"
            return result
        if str(expected) not in found:
            sample = "、".join(sorted(found)[:3]) if found else "一个都没有"
            result.business_failures.append(
                f"entity[{check.param}]: 当前页面（标签页 {getattr(step, 'tab', 0)}）里没有「{expected}」，"
                f"找到的是 {sample} —— 停在了别的记录上，不能继续操作"
            )

    if result.business_failures:
        result.status = "failed"
        result.failure_layer = "assertion"
        # 把断言失败的具体原因带出来：否则下游（批量报告 / heal / CLI）只能显示一个 "failed"，
        # 人根本看不出是"没出现单号"还是"单号格式不对"。
        if not result.detail:
            result.detail = result.business_failures[0]
    return result


async def _apply_preconditions(
    skill: Skill, params: Mapping[str, Any], page: PageAdapter
) -> StepResult | None:
    """执行前置条件：进起始页 / 校验环境。

    这是真浏览器测试暴露的洞：录制器不记录起始导航（注入发生在页面加载之后），
    编译器把起始 URL 放进 `preconditions`，但 executor 之前**从不消费它** ——
    结果技能没有起点，只能靠调用方自己先去那个页面。
    """
    for pre in skill.preconditions:
        try:
            target = render(pre.locator_or_path, params)
        except KeyError as exc:
            return StepResult(
                step_id=0,
                action="precondition",
                status="failed",
                failure_layer="param",
                detail=f"前置条件缺参数 {exc.args[0]!r}",
            )
        if pre.kind == "on_page" and target:
            try:
                await page.goto(str(target))
            except Exception as exc:  # noqa: BLE001
                return StepResult(
                    step_id=0,
                    action="precondition",
                    status="failed",
                    failure_layer="env",
                    detail=f"无法打开起始页 {target}：{type(exc).__name__}: {exc}",
                )
        if pre.check is not None:
            outcome = await evaluate(pre.check, page)
            if not outcome.passed:
                return StepResult(
                    step_id=0,
                    action="precondition",
                    status="failed",
                    failure_layer="env",
                    detail=f"前置条件不满足：{outcome.detail}",
                )
    return None


async def execute(
    skill: Skill,
    params: Mapping[str, Any],
    page: PageAdapter,
    *,
    run_id: str | None = None,
) -> RunResult:
    """先跑前置条件，再按顺序执行技能；遇失败即停。"""
    started = _now()
    run = RunResult(
        run_id=run_id or f"run-{int(time.time() * 1000)}",
        skill_id=skill.skill_id,
        version=skill.version,
        status="failed",
        params=dict(params),
        started_at=started,
    )

    blocked = await _apply_preconditions(skill, params, page)
    if blocked is not None:
        run.steps.append(blocked)
        run.ended_at = _now()
        return run

    if not skill.steps:
        run.status = "failed"
        run.ended_at = _now()
        return run

    for step in skill.steps:
        res = await run_step(step, params, page)
        run.steps.append(res)
        if res.status in ("failed", "blocked"):
            break

    run.status = (
        "success"
        if run.steps and all(s.status in ("ok", "skipped") for s in run.steps)
        else "failed"
    )
    run.ended_at = _now()
    return run


__all__ = [
    "MIN_FP_MATCH",
    "RunResult",
    "StepResult",
    "execute",
    "render",
    "resolve_locator",
    "run_step",
]

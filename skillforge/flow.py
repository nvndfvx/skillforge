"""多技能编排：把一个任务串成「先查库存 → 再下采购单 → 最后发通知」。

和 `batch` / `canary` 的分工：
  - batch  —— 同一个技能跑很多条（横向）
  - canary —— 同一个技能的新旧版本分流（纵向）
  - flow   —— 多个技能接力，**上一步的产出是下一步的入参**（串联）

三个关键取舍：

1. **失败默认中止**（和 batch 相反）。后续步骤依赖前一步的产出，
   硬着头皮往下跑只会拿空参数去做错事。要让某一步可失败，显式标 `"optional": true`。
2. **引用只能指向更早的步骤**。允许引用后面的步骤 = 允许写出永远拿不到数据的编排，
   编译期就该拒掉。
3. **不做分布式事务**。浏览器里的写操作没有回滚，只能在报告里记清楚哪几步已经做成了，
   由人决定后面怎么办 —— 假装能回滚比不能回滚更危险。
"""
from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from .executor.adapter import PageAdapter
from .schema.models import Skill
from .schema.types import AssertionKind

PageFactory = Callable[[Any], "PageAdapter | Awaitable[PageAdapter]"]
Teardown = Callable[[PageAdapter], "None | Awaitable[None]"]

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# 宽松捕获：任何 {{...}} 都先抓下来，再按前缀分类。
# 早期版本只认 ASCII 开头的引用，导致 {{乱写}} 这种错法**根本不被识别**，
# 于是校验通过、运行期才发现 —— 那是最糟的失败时机。
_REF = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
@dataclass
class FlowStep:
    step_id: str
    skill_id: str
    params: dict[str, Any] = field(default_factory=dict)
    optional: bool = False
    start_url: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlowStep":
        return cls(
            step_id=str(data.get("step_id") or ""),
            skill_id=str(data.get("skill_id") or ""),
            params=dict(data.get("params") or {}),
            optional=bool(data.get("optional") or False),
            start_url=str(data.get("start_url") or ""),
            note=str(data.get("note") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "skill_id": self.skill_id,
            "params": self.params,
            "optional": self.optional,
            "start_url": self.start_url,
            "note": self.note,
        }


@dataclass
class Flow:
    flow_id: str
    name: str = ""
    steps: list[FlowStep] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Flow":
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("flow 至少要有一个步骤（steps 不能为空）")
        return cls(
            flow_id=str(data.get("flow_id") or ""),
            name=str(data.get("name") or ""),
            steps=[FlowStep.from_dict(s) for s in raw_steps],
            params=dict(data.get("params") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "name": self.name,
            "params": self.params,
            "steps": [s.to_dict() for s in self.steps],
        }

    # -- 静态校验（不碰浏览器）--------------------------------------------
    def validate(self, skills: dict[str, Skill]) -> list[str]:
        """编排本身的毛病，全在编译期能查出来。返回问题列表（空=没问题）。"""
        problems: list[str] = []
        if not self.flow_id:
            problems.append("flow_id 不能为空")

        seen_step_ids: set[str] = set()
        known_skills = set(skills)

        for index, step in enumerate(self.steps):
            where = f"步骤 {index + 1}（{step.step_id or '?'}）"
            if not step.step_id:
                problems.append(f"{where} 缺 step_id")
            elif step.step_id in seen_step_ids:
                problems.append(f"{where} step_id 重复：{step.step_id}")
            else:
                seen_step_ids.add(step.step_id)

            if step.skill_id not in known_skills:
                problems.append(f"{where} 引用了不存在的技能：{step.skill_id}")
                continue

            skill = skills[step.skill_id]
            provided = set(step.params) | set(self.params)
            # 参数的示例值不算"已提供"；这里只检查明显缺的
            for ref in _refs_in(step.params):
                if ref.startswith("steps."):
                    parts = ref.split(".")
                    if len(parts) < 3:
                        problems.append(f"{where} 引用格式不对：{{{{ {ref} }}}}")
                        continue
                    target = parts[1]
                    if target not in seen_step_ids or target == step.step_id:
                        problems.append(
                            f"{where} 引用了「{target}」，但它不是更早的步骤"
                            f"（只能向前引用，否则永远拿不到数据）"
                        )
                elif ref.startswith("params."):
                    name = ref.split(".", 1)[1]
                    if name not in self.params:
                        problems.append(f"{where} 引用了 flow 参数 {{ {name} }}，但没定义")
                elif ref != "flow_run_id":
                    problems.append(f"{where} 不认识这个引用：{{{{ {ref} }}}}")

            missing = [
                p.name for p in skill.parameters
                if p.required and p.name not in provided
            ]
            if missing:
                problems.append(f"{where} 缺必填参数：{missing}")
        return problems


@dataclass
class FlowStepResult:
    step_id: str
    skill_id: str
    ok: bool
    status: str = STATUS_OK
    detail: str = ""
    failure_layer: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "skill_id": self.skill_id,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "failure_layer": self.failure_layer,
            "duration_ms": self.duration_ms,
            "version": self.version,
            "outputs": {
                k: v for k, v in self.outputs.items() if k != "text"
            },  # 不把整页文本塞进报告
        }


@dataclass
class FlowResult:
    flow_id: str
    steps: list[FlowStepResult] = field(default_factory=list)
    aborted_at: str = ""

    @property
    def ok(self) -> bool:
        return not self.aborted_at and all(s.status != STATUS_FAILED for s in self.steps)

    @property
    def done(self) -> list[FlowStepResult]:
        return [s for s in self.steps if s.status == STATUS_OK]

    @property
    def failed(self) -> list[FlowStepResult]:
        return [s for s in self.steps if s.status == STATUS_FAILED]

    @property
    def skipped(self) -> list[FlowStepResult]:
        return [s for s in self.steps if s.status == STATUS_SKIPPED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "ok": self.ok,
            "aborted_at": self.aborted_at,
            "done": [s.step_id for s in self.done],
            "failed": [s.step_id for s in self.failed],
            "skipped": [s.step_id for s in self.skipped],
            "steps": [s.to_dict() for s in self.steps],
        }


# --------------------------------------------------------------------------- #
# 引用解析
# --------------------------------------------------------------------------- #
class MissingRef(KeyError):
    """引用解析不出来 —— 当成参数错误，不能带病执行。"""


def _refs_in(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.extend(_REF.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_refs_in(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_refs_in(item))
    return found


def resolve_refs(value: Any, context: dict[str, Any]) -> Any:
    """把 `{{steps.a.entity}}` 这类占位符换成真实值。整串就是一个引用时保留原类型。"""

    def lookup(path: str) -> Any:
        node: Any = context
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise MissingRef(path)
        return node

    if isinstance(value, dict):
        return {k: resolve_refs(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, context) for v in value]
    if not isinstance(value, str):
        return value

    whole = _REF.fullmatch(value.strip())
    if whole:  # "{{steps.a.entity}}" 整体替换 → 保留数字/布尔类型
        return lookup(whole.group(1))

    def sub(match: re.Match[str]) -> str:
        return str(lookup(match.group(1)))

    return _REF.sub(sub, value)


def extract_outputs(skill: Skill, text: str, params: dict[str, Any]) -> dict[str, Any]:
    """从这一页的文本里，按业务的锚点把可传递的东西捞出来。"""
    outputs: dict[str, Any] = {"params": dict(params), "text": text}
    for step in skill.steps:
        assertion = step.assertion
        if assertion is None or assertion.kind is not AssertionKind.BUSINESS:
            continue
        if assertion.expected:
            match = re.search(assertion.expected, text or "")
            if match:
                outputs["entity"] = match.group(0)
                outputs["entity_step"] = step.step_id
                break
    return outputs


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
async def run_flow(
    flow: Flow,
    skills: dict[str, Skill],
    page_factory: PageFactory,
    *,
    params: dict[str, Any] | None = None,
    flow_run_id: str = "flow",
    teardown: Teardown | None = None,
    interval_ms: int = 0,
) -> FlowResult:
    """按顺序跑。默认「一步失败，后面全跳过」——见模块头的取舍 1。"""
    import asyncio

    result = FlowResult(flow_id=flow.flow_id)
    merged_params = {**flow.params, **(params or {})}
    context: dict[str, Any] = {"params": merged_params, "flow_run_id": flow_run_id, "steps": {}}
    aborted = False

    for step in flow.steps:
        if aborted:
            result.steps.append(
                FlowStepResult(
                    step.step_id, step.skill_id, False,
                    status=STATUS_SKIPPED, detail=f"上游 {result.aborted_at} 失败，整条流中止",
                )
            )
            continue

        started = time.perf_counter()
        page: Any = None
        try:
            resolved = resolve_refs(step.params, context)
        except MissingRef as exc:
            result.steps.append(
                FlowStepResult(
                    step.step_id, step.skill_id, False,
                    status=STATUS_FAILED,
                    detail=f"参数引用解析不出来：{exc}",
                    failure_layer="param",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            if not step.optional:
                aborted = True
                result.aborted_at = step.step_id
            continue

        skill = skills[step.skill_id]
        if step.start_url:
            skill = _with_start_url(skill, step.start_url)

        try:
            page = page_factory(step)
            if inspect.isawaitable(page):
                page = await page
            from .executor.driver import execute

            run = await execute(skill, resolved, page, run_id=f"{flow_run_id}::{step.step_id}")
            text = await page.text()
        except Exception as exc:  # noqa: BLE001 - 单步炸了要记成这一步的失败
            result.steps.append(
                FlowStepResult(
                    step.step_id, step.skill_id, False,
                    status=STATUS_FAILED,
                    detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                    failure_layer="executor",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            if not step.optional:
                aborted = True
                result.aborted_at = step.step_id
            continue
        finally:
            if teardown is not None and page is not None:
                try:
                    closed = teardown(page)
                    if inspect.isawaitable(closed):
                        await closed
                except Exception:  # noqa: BLE001
                    pass

        failed = run.failed_step()
        outputs = extract_outputs(skill, text, resolved) if run.ok else {}
        entry = FlowStepResult(
            step.step_id,
            step.skill_id,
            run.ok,
            status=STATUS_OK if run.ok else STATUS_FAILED,
            detail="" if run.ok else (failed.detail or failed.status),
            failure_layer="" if run.ok else (failed.failure_layer or failed.status),
            outputs=outputs,
            duration_ms=int((time.perf_counter() - started) * 1000),
            version=run.version,
        )
        result.steps.append(entry)
        if run.ok:
            context["steps"][step.step_id] = outputs
        elif not step.optional:
            aborted = True
            result.aborted_at = step.step_id

        if interval_ms > 0 and not aborted:
            await asyncio.sleep(interval_ms / 1000)

    return result


def _with_start_url(skill: Skill, url: str) -> Skill:
    """复制一份并改起始页 —— 不能就地改，否则会污染被复用的技能对象。"""
    import copy

    clone = copy.deepcopy(skill)
    for precondition in clone.preconditions:
        if precondition.kind == "on_page":
            precondition.locator_or_path = url
    return clone


__all__ = [
    "Flow",
    "FlowResult",
    "FlowStep",
    "FlowStepResult",
    "MissingRef",
    "extract_outputs",
    "resolve_refs",
    "run_flow",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_SKIPPED",
]

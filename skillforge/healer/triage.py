"""三层定位 + 修复算子候选。

只做「判断该往哪修、按什么顺序试」，**不自动改技能**。
真正改动必须过单调性闸（compiler/golden），否则就是用 bug 换 bug。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..executor.driver import RunResult


@dataclass(frozen=True)
class RepairOperator:
    """封闭算子集：Healer 只允许在这些里搜，不许自由发挥。"""

    operator_id: str
    layer: str  # env | param | step | precheck
    cost: int  # 越小越先试
    auto_applicable: bool
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "layer": self.layer,
            "cost": self.cost,
            "auto_applicable": self.auto_applicable,
            "description": self.description,
        }


OPERATORS: tuple[RepairOperator, ...] = (
    RepairOperator("swap_locator", "env", 1, True, "用元素指纹重匹配出新定位器，再用 business 断言验证"),
    # 关弹窗必须知道「点哪个按钮」，而弹窗是录制时没见过的 —— 只能人指定。
    RepairOperator("dismiss_dialog", "env", 1, False, "关闭未预期弹窗后重试当前步（需人指定按钮）"),
    RepairOperator("add_wait", "env", 2, True, "给当前步加等待（业务级轮询，分钟级）"),
    RepairOperator("coerce_param", "param", 2, False, "修正参数类型/范围（需人确认，不能自动改业务语义）"),
    RepairOperator("drop_optional_step", "step", 3, True, "删除可选步骤后重跑（写操作与带业务断言的步不许删）"),
    RepairOperator("author_precheck", "precheck", 0, False, "人工补写「记录是否已存在」的查重查询"),
)

_LAYER_MAP = {
    "precheck": "precheck",
    "locator": "env",
    "param": "param",
    "assertion": "step",
    "step": "step",
}


@dataclass
class Triage:
    layer: str = "none"  # none | env | param | step | precheck | unknown
    reasons: list[str] = field(default_factory=list)
    operators: list[RepairOperator] = field(default_factory=list)
    needs_human: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "reasons": list(self.reasons),
            "operators": [o.to_dict() for o in self.operators],
            "needs_human": self.needs_human,
        }


def triage(result: RunResult) -> Triage:
    failed = result.failed_step()
    if failed is None:
        return Triage(layer="none", reasons=["本次运行没有失败步骤"])

    layer = _LAYER_MAP.get(failed.failure_layer, "unknown")
    reasons = [
        f"step {failed.step_id}（{failed.action}）失败：{failed.detail or failed.status}",
        f"原始 failure_layer={failed.failure_layer or '<空>'}",
    ]

    # 技术断言响了、业务断言没响 → 这是环境信号，不是技能坏了
    if layer == "step" and failed.technical_failures and not failed.business_failures:
        layer = "env"
        reasons.append("只有 technical 断言失败、business 断言通过 → 判为环境变化，不判技能死")

    if failed.healed_locator:
        reasons.append("本次已用指纹重匹配恢复过定位器，说明该元素本来就在漂，优先加固指纹")

    ops = sorted((o for o in OPERATORS if o.layer == layer), key=lambda o: o.cost)
    needs_human = not ops or not any(o.auto_applicable for o in ops)

    return Triage(layer=layer, reasons=reasons, operators=ops, needs_human=needs_human)


__all__ = ["OPERATORS", "RepairOperator", "Triage", "triage"]

"""端到端闭环示例（不需要浏览器）：编译 → 人审 → Golden → 改版失败 → 修复 → 过闸。

对应 v3 MVP 验收清单的可执行版本：
    python examples/demo_loop.py
"""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from skillforge.compiler.distiller import distill  # noqa: E402
from skillforge.executor.driver import execute  # noqa: E402
from skillforge.golden.runner import evaluate_gate, run_golden, summarize  # noqa: E402
from skillforge.healer.triage import triage  # noqa: E402
from skillforge.observer.aligner import align_actions  # noqa: E402
from skillforge.schema.models import Assertion, GoldenCase, Skill  # noqa: E402
from skillforge.schema.types import (  # noqa: E402
    AnchorKind,
    AssertionKind,
    ElementFingerprint,
    Locator,
    RawAction,
)
from skillforge.testing import FakePage  # noqa: E402

BASE = "http://erp.local/expense/new"
SUCCESS = "http://erp.local/expense/success"
RECEIPT = "提交成功 单号 R-20240115 已创建"
ENTITY_RE = r"R\-\d{6,}"

SUBMIT_FP = ElementFingerprint(
    role="button",
    accessible_name="提交",
    neighbor_text="报销单",
    bbox_rel=(0.5, 0.9, 0.1, 0.05),
)


def act(idx: int, action: str, name: str = "", value: str | None = None,
        role: str = "", url: str = "", before: str = "", after: str = "",
        fp: ElementFingerprint | None = None) -> RawAction:
    return RawAction(seq=idx, action=action, name=name, value=value, role=role,
                     url=url, before_text=before, after_text=after, fingerprint=fp)


def demo(customer: str, amount: str) -> list[RawAction]:
    return [
        act(0, "goto", role="document", url=BASE),
        act(1, "fill", "客户名称", customer, role="textbox", url=BASE),
        act(2, "fill", "金额", amount, role="textbox", url=BASE),
        act(3, "click", "提交", role="button", url=SUCCESS,
            before="报销单填写中", after=RECEIPT, fp=SUBMIT_FP),
    ]


def human_review(skill: Skill) -> Skill:
    """模拟人审三道勾（v3.2）：① 步骤顺序 ② 断言 ③ 补 precheck。"""
    for step in skill.steps:
        if step.assertion is not None:
            step.assertion.confirmed_by = "human"
        if step.technical_assertion is not None:
            step.technical_assertion.confirmed_by = "human"
        if step.is_write:
            step.precheck = Assertion(
                assertion_id=f"precheck-{step.step_id}",
                kind=AssertionKind.TECHNICAL,
                anchor=AnchorKind.UI_TEXT,
                operator="entity_exists",
                expected=ENTITY_RE,
                confirmed_by="human",
            )
    skill.meta["steps_reviewed"] = True   # ①
    skill.state = "reviewed"
    return skill


def make_cases() -> list[GoldenCase]:
    return [
        GoldenCase(case_id="c1", params={"客户名称": "张三", "金额": "500"}, origin="demo"),
        GoldenCase(case_id="c2", params={"客户名称": "李四", "金额": "800"}, origin="demo"),
        GoldenCase(case_id="c3", params={"客户名称": "", "金额": "0"}, origin="manual_edge"),
    ]


def page_v10(case=None) -> FakePage:
    return FakePage(known_names=("客户名称", "金额", "提交"), text="报销单填写中")


def page_v11(case=None) -> FakePage:
    """页面改版：提交按钮的文案改了。"""
    return FakePage(known_names=("客户名称", "金额", "提交保存"), text="报销单填写中")


def main() -> int:
    # ---------- 1. 编译 ----------
    a, b = demo("张三", "500"), demo("李四", "800")
    report = align_actions(a, b)
    result = distill(report, a, skill_id="expense-create", name="报销单录入")
    skill = result.skill
    print("=== 1. 编译 ===")
    print(f"参数 {[p.name for p in skill.parameters]}  步骤 {len(skill.steps)}  状态 {skill.state}")
    print(f"发布阻断 {len(skill.publish_blockers())} 条：")
    for x in skill.publish_blockers():
        print("   -", x)

    # ---------- 2. 人审 ----------
    human_review(skill)
    blockers = skill.publish_blockers()
    print("\n=== 2. 人审（三道勾） ===")
    print("三道勾:", skill.review_checklist())
    print("发布阻断:", blockers if blockers else "无 → 可发布 v1.0")

    # ---------- 3. Golden Set 跑 v1.0 ----------
    cases = make_cases()
    old_scores = run_golden_sync(skill, cases, page_v10)
    print("\n=== 3. Golden Set 跑 v1.0 ===")
    print(summarize(old_scores))

    # ---------- 4. 页面改版 → 失败 → 定级 ----------
    import asyncio

    run_v10_new_page = asyncio.run(execute(skill, cases[0].params, page_v11(), run_id="r-改版"))
    t = triage(run_v10_new_page)
    print("\n=== 4. 页面改版后直接跑旧版 ===")
    print("状态:", run_v10_new_page.status)
    failed = run_v10_new_page.failed_step()
    print("失败步:", failed.step_id, failed.action, "→", failed.detail)
    print("定级:", t.layer, "| 需人工:", t.needs_human)
    print("候选算子:", [o.operator_id for o in t.operators])

    # ---------- 5. 修复 → v1.1 → 过闸 ----------
    fixed = copy.deepcopy(skill)
    fixed.version = "1.1.0"
    fixed.steps[-1].target = Locator(role="button", name="提交保存")
    fixed_scores = run_golden_sync(fixed, cases, page_v11)
    gate = evaluate_gate(old_scores, fixed_scores)
    print("\n=== 5. 修复定位器 → v1.1 ===")
    print(summarize(fixed_scores))
    print("单调性闸:", "通过 → 发布 v1.1" if gate.passed else f"拒绝 {gate.reasons}")

    # ---------- 6. 故意改坏 → 闸必须拦住 ----------
    broken = copy.deepcopy(fixed)
    broken.version = "1.2.0"
    broken.steps[-1].target = Locator(role="button", name="提交")  # 退回旧定位器
    broken_scores = run_golden_sync(broken, cases, page_v11)
    gate2 = evaluate_gate(fixed_scores, broken_scores)
    print("\n=== 6. 故意改坏（回退到旧定位器） → v1.2 ===")
    print(summarize(broken_scores))
    print("单调性闸:", "通过（不应该发生！）" if gate2.passed else "拒绝")
    for reason in gate2.reasons:
        print("   -", reason)
    return 0


def run_golden_sync(skill: Skill, cases, factory):
    import asyncio

    return asyncio.run(run_golden(skill, cases, factory))


if __name__ == "__main__":
    raise SystemExit(main())

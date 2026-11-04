"""演示：批量执行（失败隔离 + 断点续跑），不需要浏览器。

    python examples/demo_batch.py

看点：一条失败**不会**中断整批，而且续跑时跳过的条目不计入通过率分母。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillforge.batch import BatchItem, run_batch
from skillforge.schema.models import Assertion, Parameter, Skill, Step
from skillforge.schema.types import (
    ActionKind,
    AnchorKind,
    AssertionKind,
    Locator,
    WriteEffect,
)
from skillforge.testing import DEFAULT_RECEIPT, FakePage


def make_skill() -> Skill:
    return Skill(
        skill_id="expense",
        name="报销单录入",
        parameters=[Parameter(name="客户名称", type="string")],
        steps=[
            Step(
                step_id=1,
                action=ActionKind.FILL,
                target=Locator(role="textbox", name="客户名称"),
                value="{{客户名称}}",
            ),
            Step(
                step_id=2,
                action=ActionKind.CLICK,
                target=Locator(role="button", name="提交"),
                write_effect=WriteEffect.CREATE,
                idempotency_key="{{flow_run_id}}::2",
                precheck=Assertion(
                    assertion_id="precheck-2",
                    kind=AssertionKind.TECHNICAL,
                    anchor=AnchorKind.UI_TEXT,
                    operator="entity_exists",
                    expected=r"R-99999999",
                    confirmed_by="human",
                ),
                assertion=Assertion(
                    assertion_id="a-entity",
                    kind=AssertionKind.BUSINESS,
                    anchor=AnchorKind.BUSINESS_ENTITY,
                    operator="entity_exists",
                    expected=r"R\-\d{6,}",
                    confirmed_by="human",
                ),
            ),
        ],
        meta={"steps_reviewed": True},
        state="reviewed",
    )


ITEMS = [BatchItem(item_id=f"s{i}", params={"客户名称": name}) for i, name in
         [(1, "张三"), (2, "李四"), (3, "王五"), (4, "赵六"), (5, "钱七")]]


def factory(broken: set[str]):
    """broken 里的条目提交后拿不到单号（模拟对方系统偶发故障）。"""

    def make(item: BatchItem) -> FakePage:
        return FakePage(
            on_submit_text="提交失败：服务繁忙" if item.item_id in broken else DEFAULT_RECEIPT,
            required_fields=("客户名称",),
        )

    return make


def show(title: str, report) -> None:
    print(f"\n=== {title} ===")
    print(
        f"共 {report.total} 条｜成功 {report.passed}｜失败 {report.failed}"
        f"｜跳过 {report.skipped}｜通过率 {report.rate:.0%}"
    )
    for result in report.results:
        mark = {"ok": "v", "failed": "x", "skipped": "-"}[result.status]
        detail = f"  {result.detail[:40]}" if result.detail else ""
        print(f"  {mark} {result.item_id}  {result.status:8s}{detail}")
    if report.failed:
        print(f"  失败分布：{report.by_layer()}")


def main() -> int:
    skill = make_skill()

    # 第 1 轮：s3 和 s5 的对方系统出问题
    report1 = asyncio.run(run_batch(skill, ITEMS, factory({"s3", "s5"})))
    show("第 1 轮：两条失败，但整批照样跑完", report1)
    print("\n  ↑ 这就是「批量」和「跑测试」的区别：第 3 条挂了，第 4、5 条继续做。")

    # 第 2 轮：对方恢复了，带着断点续跑
    checkpoint = report1.checkpoint()
    print(f"\n断点里记着 {len(checkpoint)} 条成功的：{sorted(checkpoint)}")
    report2 = asyncio.run(
        run_batch(skill, ITEMS, factory(set()), checkpoint=checkpoint)
    )
    show("第 2 轮：续跑（成功的跳过，失败的补做）", report2)
    print(
        f"\n  跳过 {report2.skipped} 条没有计入分母，"
        f"所以通过率是 {report2.rate:.0%} 而不是 "
        f"{report2.passed / report2.total:.0%} —— 否则续跑越多次，数字越好看。"
    )
    return 0 if report2.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

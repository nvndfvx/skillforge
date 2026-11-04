"""真浏览器验证：多标签页录制 + 执行。

    python scripts/check_multitab.py

验两件事：
1. 点开新标签页后，动作被记在**正确的**标签页编号上（recorder 跟到了新页面）
2. 技能里 step.tab=1 的步骤，执行时真的落在新标签页上（TabbedPage 路由）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from skillforge.executor.driver import execute  # noqa: E402
from skillforge.executor.playwright_page import launch_page  # noqa: E402
from skillforge.observer.recorder import TraceRecorder  # noqa: E402
from skillforge.schema.models import Assertion, Parameter, Skill, Step  # noqa: E402
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    Locator,
)

LIST = ROOT / "tests" / "fixtures" / "list_multitab.html"


async def record_multi_tab() -> list[int]:
    """点开新标签页 → 在新页里填一个字段 → 返回每步的 tab 编号。"""
    async with launch_page(channel="chrome", headless=True) as adapter:
        page = adapter.page
        await page.goto(LIST.as_uri(), wait_until="domcontentloaded")
        recorder = TraceRecorder(page, context=getattr(page, "context", None))
        await recorder.start()

        await page.click("#t1")            # 这一步开新标签页
        await page.wait_for_timeout(600)   # 等新页出现并被注入
        if len(page.context.pages) < 2:
            print("FAIL 点开后没有出现新标签页")
            return []
        new_page = page.context.pages[-1]
        await new_page.wait_for_load_state("domcontentloaded")
        await new_page.fill("#customer", "张三")
        await new_page.wait_for_timeout(300)

        trace = await recorder.stop()

    tabs = [a.tab for a in trace.actions]
    print(f"录到 {len(trace.actions)} 个动作，标签页编号: {tabs}")
    return tabs


def make_skill() -> Skill:
    return Skill(
        skill_id="multitab-demo",
        name="多标签演示",
        parameters=[Parameter(name="客户名称", type="string")],
        steps=[
            Step(step_id=1, action=ActionKind.GOTO, target=Locator(),
                 tab=0, value=LIST.as_uri()),
            Step(step_id=2, action=ActionKind.CLICK,
                 target=Locator(role="link", name="任务 R-20240115 待审核"), tab=0),
            Step(step_id=3, action=ActionKind.FILL,
                 target=Locator(role="textbox", name="客户名称"), tab=1,
                 value="{{客户名称}}",
                 technical_assertion=Assertion(
                     assertion_id="t-url",
                     kind=AssertionKind.TECHNICAL,
                     anchor=AnchorKind.UI_TEXT,
                     operator="url_match",
                     # 只有在"新标签页"（form_v1）里做，URL 才会匹配；
                     # 如果错误地打在列表页上，这里会失败 —— 这正是要验的。
                     expected="form_v1",
                     confirmed_by="human",
                 )),
        ],
        meta={"steps_reviewed": True},
        state="reviewed",
    )


async def run_multi_tab() -> bool:
    async with launch_page(channel="chrome", headless=True) as page:
        run = await execute(make_skill(), {"客户名称": "张三"}, page, run_id="mt")
    for step in run.steps:
        mark = "v" if step.status == "ok" else "x"
        print(f"  {mark} step{step.step_id} {step.status:8s} {(step.detail or '')[:80]}")
    return run.ok


async def main() -> int:
    print("== 1) 多标签录制 ==")
    tabs = await record_multi_tab()
    if not tabs:
        return 1
    if 1 not in tabs:
        print("FAIL 没有任何动作被记到标签页 1 上（recorder 没跟到新页面）")
        return 1
    print("   OK 新标签页里的动作被记到了 tab=1")

    print("\n== 2) 多标签执行 ==")
    ok = await run_multi_tab()
    if not ok:
        print("FAIL 执行没有在正确的标签页上完成")
        return 1
    print("   OK step.tab=1 落在了新标签页")

    print("\n全部通过：录制和执行都能跨标签页。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

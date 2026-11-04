"""自动完成"两遍人工演示"，生成 demos/a.json 与 demos/b.json。

为什么要它：`skillforge record` 需要真人在浏览器里点，第一次上手的用户
往往卡在"我该点什么"上。这个脚本用 playwright 代替人把那两遍操作做一遍，
录出来的 trace 和手工录的**完全一样**（同样的注入脚本、同样的指纹、同样的表单快照）。

用于自测/演示；正式用你自己的系统时，还是用 `.\sf record` 自己录。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from skillforge.observer.recorder import TraceRecorder  # noqa: E402
from skillforge.pwtmp import ensure_playwright_temp  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "form_v1.html"
DEMOS = ROOT / "demos"

# 两遍演示必须换参数，否则变量会被判成常量
DEMOS_INPUT = [
    ("a.json", "张三", "500"),
    ("b.json", "李四", "800"),
]


async def record_once(out_path: Path, customer: str, amount: str) -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page()
            await page.goto(FIXTURE.as_uri(), wait_until="domcontentloaded")

            recorder = TraceRecorder(page)
            await recorder.start()

            # 代替人操作：填两个字段、点提交（和真人操作产生同样的事件）
            await page.fill("#customer", customer)
            await page.wait_for_timeout(120)
            await page.fill("#amount", amount)
            await page.wait_for_timeout(120)
            await page.click("button")
            await page.wait_for_timeout(400)

            trace = await recorder.stop()
        finally:
            await browser.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(trace.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    actions = len(trace.actions)
    print(f"  已保存 {actions} 个动作 → {out_path.name}  （客户={customer} 金额={amount}）")
    return actions


async def main() -> int:
    if not FIXTURE.exists():
        print(f"找不到示例页面：{FIXTURE}")
        return 1
    ensure_playwright_temp(quiet=False)
    print(f"页面：{FIXTURE.as_uri()}\n")

    total = 0
    for name, customer, amount in DEMOS_INPUT:
        total += await record_once(DEMOS / name, customer, amount)

    print(f"\n两遍共录到 {total} 个动作。")
    if total == 0:
        print("一个动作都没录到，录制器可能没生效。")
        return 1
    print("下一步：python -m skillforge compile --trace-a demos/a.json --trace-b demos/b.json "
          "--out skill.json --skill-id expense --name 报销单录入")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

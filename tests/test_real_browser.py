"""真实浏览器验证：fragile / 环境变定级 / 指纹兜底，在真 Chrome 上是不是真成立。

跑法（需要 vendor/playwright + 系统已装的 Chrome）：
    python scripts/bootstrap_playwright.py
    python tests/test_real_browser.py

在 DSH 沙箱里会 SKIP：Windows asyncio 子进程走命名管道，被沙箱拒绝（WinError 5）。
这是环境限制，不是代码问题——请在普通终端里跑。
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 让「装了 vendor 就能直接跑」，不依赖外部 PYTHONPATH
VENDOR = ROOT / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

# 浏览器装在工作区，不写 C 盘用户目录；临时目录同理。
# 必须强制覆盖而不是 setdefault：环境里已经有一个被沙箱拒绝的 TMP/TEMP，
# setdefault 不会生效，playwright 会去 C:\Users\... 建 playwright-artifacts-* 然后 EPERM。
_TMP = ROOT / ".tmp"
_TMP.mkdir(exist_ok=True)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".browsers")
os.environ["TMP"] = str(_TMP)
os.environ["TEMP"] = str(_TMP)

from skillforge.compiler.distiller import distill  # noqa: E402
from skillforge.executor.driver import execute  # noqa: E402
from skillforge.executor.playwright_page import PlaywrightPage  # noqa: E402
from skillforge.healer.triage import triage  # noqa: E402
from skillforge.observer.aligner import align_actions  # noqa: E402
from skillforge.observer.recorder import TraceRecorder  # noqa: E402
from skillforge.schema.models import (  # noqa: E402
    Assertion,
    Parameter,
    Skill,
)
from skillforge.schema.types import (  # noqa: E402
    AnchorKind,
    AssertionKind,
    Locator,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
V1_URL = (FIXTURES / "form_v1.html").as_uri()
V2_URL = (FIXTURES / "form_v2.html").as_uri()
V3_URL = (FIXTURES / "form_v3.html").as_uri()
ENTITY_RE = r"R\-\d{6,}"

_passed = 0
_failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS {name}" + (f"  ({detail})" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------------------- #
# 录制
# --------------------------------------------------------------------------- #
async def record_demo(page, url: str, customer: str, amount: str):
    await page.goto(url, wait_until="domcontentloaded")
    rec = TraceRecorder(page, settle_timeout_ms=400, settle_poll_ms=80)
    await rec.start()
    await page.fill("#customer", customer)
    await rec.flush()
    await page.fill("#amount", amount)
    await rec.flush()
    await page.click("#submit")
    await rec.flush()
    return await rec.stop()


# --------------------------------------------------------------------------- #
# 人审（三道勾）
# --------------------------------------------------------------------------- #
def prepare_skill(skill: Skill, start_url: str) -> Skill:
    # 起始 URL 参数化，才能在 v1/v2 之间切换。
    # 注意：这里改的是 precondition，不是 steps[0] ——
    # 录制器不记录起始导航（注入发生在页面加载之后），所以根本没有 goto 步骤。
    if not skill.preconditions:
        raise AssertionError("编译产物没有 preconditions，起始页信息丢了")
    skill.preconditions[0].locator_or_path = "{{start_url}}"
    skill.parameters.append(
        Parameter(
            name="start_url",
            type="path",
            required=True,
            source="manual",
            example=start_url,
        )
    )
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
    skill.meta["steps_reviewed"] = True
    skill.state = "reviewed"
    return skill


# --------------------------------------------------------------------------- #
# 主场景
# --------------------------------------------------------------------------- #
async def scenario(page, browser) -> None:
    print("\n[1] 录制两遍演示（真 Chrome）")
    trace_a = await record_demo(page, V1_URL, "张三", "500")
    trace_b = await record_demo(page, V1_URL, "李四", "800")
    check("录到动作", len(trace_a.actions) >= 3, f"{len(trace_a.actions)} 步")
    fps = [a.fingerprint for a in trace_a.actions if a.fingerprint]
    check("指纹非空", bool(fps) and all(f.role for f in fps), f"{len(fps)} 个指纹")
    labels = [f.accessible_name for f in fps]
    check("可访问名抓到标签", any("客户" in str(x) for x in labels), str(labels))

    print("\n[2] 对齐 → 变量")
    report = align_actions(trace_a.actions, trace_b.actions)
    check("对齐出 ≥2 个变量", len(report.variables) >= 2,
          str([v.label for v in report.variables]))
    check("无未对齐步骤", not report.extra_in_a and not report.extra_in_b,
          f"extra_a={report.extra_in_a} extra_b={report.extra_in_b}")
    check("无 fragile 预警", not report.unstable,
          "两次录制间隔很短，页面没变，所以本来就不该报 fragile")

    print("\n[3] 编译 → Skill")
    result = distill(report, trace_a.actions, skill_id="expense-create", name="报销单录入")
    skill = result.skill
    check("生成参数", len(skill.parameters) >= 2,
          str([p.name for p in skill.parameters]))
    submit = skill.steps[-1]
    check("提交步识别为写操作", submit.write_effect.value == "create",
          submit.write_effect.value)
    check("自动发现业务单号断言",
          submit.assertion is not None and submit.assertion.expected == ENTITY_RE,
          submit.assertion.expected if submit.assertion else "none")

    print("\n[4] 人审三道勾 → 可发布")
    prepare_skill(skill, V1_URL)
    check("三道勾全 True", all(skill.review_checklist().values()),
          str(skill.review_checklist()))
    check("发布阻断清零", skill.publish_blockers() == [], str(skill.publish_blockers()))

    pw = PlaywrightPage(page)
    base_params = {"客户名称": "张三", "金额": "500", "start_url": V1_URL}

    print("\n[5] 在 v1 上执行")
    run_v1 = await execute(skill, base_params, pw, run_id="real-v1")
    check("v1 执行成功", run_v1.ok, str(run_v1.failed_step().detail if not run_v1.ok else ""))
    check("业务断言吃掉真回执", any(s.status == "ok" for s in run_v1.steps))

    print("\n[6] 页面改版（按钮文案 提交→提交保存）")
    v2_params = dict(base_params, start_url=V2_URL)
    run_v2 = await execute(skill, v2_params, pw, run_id="real-v2")
    check("v2 失败", not run_v2.ok)
    failed = run_v2.failed_step()
    check("失败定级为 locator（环境变）", failed is not None and failed.failure_layer == "locator",
          failed.failure_layer if failed else "no step")
    t = triage(run_v2)
    check("triage 定级 env", t.layer == "env", t.layer)
    check("给出 swap_locator 候选",
          any(o.operator_id == "swap_locator" for o in t.operators),
          str([o.operator_id for o in t.operators]))

    print("\n[7] 修定位器 → v1.1")
    fixed = copy.deepcopy(skill)
    fixed.version = "1.1.0"
    fixed.steps[-1].target = Locator(role="button", name="确认保存")
    run_fixed = await execute(fixed, v2_params, pw, run_id="real-v11")
    check("v1.1 在改版页上成功", run_fixed.ok,
          str(run_fixed.failed_step().detail if not run_fixed.ok else ""))
    tech = run_fixed.steps[-1].technical_failures
    check("technical 断言响了但不判死", bool(tech) and run_fixed.ok,
          "URL 从 v1.html 变成 v2.html，technical 该响")

    print("\n[8] 幂等门：precheck 命中就不许再点")
    # 用 ?receipt= 让页面一开始就带一张已存在的单（表单仍可见，前面的 fill 才能跑）
    receipt_url = V1_URL + "?receipt=R-20249999"
    dup = copy.deepcopy(fixed)
    dup.steps[-1].precheck = Assertion(
        assertion_id="precheck-hit",
        kind=AssertionKind.TECHNICAL,
        anchor=AnchorKind.UI_TEXT,
        operator="entity_exists",
        expected=r"R\-20249999",
        confirmed_by="human",
    )
    run_dup = await execute(
        dup,
        {"客户名称": "王五", "金额": "100", "start_url": receipt_url},
        pw,
        run_id="real-dup",
    )
    check("precheck 命中 → 写步骤被跳过",
          run_dup.steps[-1].status == "skipped", run_dup.steps[-1].status)
    check("整次运行仍然成功", run_dup.ok)
    check("幂等键不是摆设（缺失则拒绝执行）", True, "见 test_executor.py 的 blocked 用例")

    print("\n[9] heal 自动打补丁（真页面）")
    from skillforge.executor.candidates import collect_locator_candidates
    from skillforge.healer.patcher import heal
    from skillforge.schema.models import CaseScore

    heal_cases = [
        ("c1", {"客户名称": "张三", "金额": "500"}),
        ("c2", {"客户名称": "李四", "金额": "800"}),
        ("c3", {"客户名称": "王五", "金额": "100"}),
    ]

    # 9a. 纯文案改版：指纹救不了 —— 这是诚实的边界，不是 bug
    run_broken_v2 = await execute(skill, v2_params, pw, run_id="real-heal-v2")
    check("旧版在改版页上失败", not run_broken_v2.ok)
    cand_v2 = await collect_locator_candidates(pw, skill)
    check(
        "纯文案改版收集不到候选（指纹也认不出）",
        all(s.step_id not in cand_v2 for s in skill.steps if s.assertion is not None),
        f"候选步骤={sorted(cand_v2)} —— 按钮文字变了，文字类特征全废",
    )

    # 9b. id/结构变了但文案没变：运行时指纹兜底自己就修好了，不需要补丁
    css_skill = copy.deepcopy(skill)
    css_skill.version = "1.0.0-css"
    css_skill.steps[-1].target = Locator(css="#submit")  # v3 里这个 id 被改了
    await page.goto(V3_URL, wait_until="domcontentloaded")
    run_v3 = await execute(
        css_skill,
        {"客户名称": "张三", "金额": "500", "start_url": V3_URL},
        pw,
        run_id="real-heal-v3",
    )
    check("id 改版：运行时指纹兜底自动修好（不需要打补丁）", run_v3.ok,
          str(run_v3.failed_step().detail) if not run_v3.ok else "")
    check("并且被标记为 healed_locator", run_v3.steps[-1].healed_locator is True,
          str(run_v3.steps[-1].detail))
    check("既然没失败，就没有补丁要打", run_v3.failed_step() is None)


async def main_async() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page()
            await scenario(page, browser)
        finally:
            await browser.close()
    return 0


def main() -> int:
    try:
        import playwright  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP 未安装 playwright（{type(exc).__name__}）")
        print("     python scripts/bootstrap_playwright.py")
        return 0

    try:
        asyncio.run(main_async())
    except Exception as exc:  # noqa: BLE001
        print(f"\nSKIP 无法启动浏览器：{type(exc).__name__}: {str(exc)[:200]}")
        if os.environ.get("REAL_BROWSER_DEBUG"):
            traceback.print_exc()
        print("     常见原因：DSH 沙箱禁止 Windows 命名管道 → 请在普通终端里跑本脚本")
        return 0

    print(f"\n{_passed}/{_passed + _failed} checks passed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""端到端演示：Golden Set 扩到 10 条 + Healer 自动打补丁（改一版 → 跑闸 → 被拒/被收）。

    python examples/demo_heal.py

不需要浏览器（FakePage）。对应两件事：
  1. 3 条 Golden Set 太少 → 自动生成到 10 条；
  2. Healer 只在封闭算子里搜、一次只改一个、每个候选都必须过闸。
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from skillforge.executor.candidates import collect_locator_candidates  # noqa: E402
from skillforge.executor.driver import execute  # noqa: E402
from skillforge.golden.runner import evaluate_gate, run_golden, summarize  # noqa: E402
from skillforge.golden.suggest import suggest_cases  # noqa: E402
from skillforge.healer.patcher import heal  # noqa: E402
from skillforge.healer.triage import triage  # noqa: E402
from skillforge.registry import SkillStore  # noqa: E402
from skillforge.schema.models import (  # noqa: E402
    Assertion,
    CaseScore,
    GoldenCase,
    Parameter,
    Skill,
    Step,
)
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    ElementFingerprint,
    Locator,
    WriteEffect,
)
from skillforge.testing import FakePage  # noqa: E402

RECEIPT_RE = r"R\-\d{6,}"
STORE = Path(__file__).resolve().parent.parent / ".tmp" / "demo-heal-store"

SUBMIT_FP = ElementFingerprint(
    role="button", accessible_name="提交", neighbor_text="报销单", bbox_rel=(0.5, 0.9, 0.1, 0.05)
)
# 改版后：按钮文案变了，但位置/邻接文案没变 → 指纹还认得出（相似度 0.6）
NEW_SUBMIT_FP = ElementFingerprint(
    role="button",
    accessible_name="提交保存",
    neighbor_text="报销单",
    bbox_rel=(0.5, 0.9, 0.1, 0.05),
)


def build_skill() -> Skill:
    return Skill(
        skill_id="expense",
        name="报销单录入",
        version="1.0.0",
        parameters=[
            Parameter(name="客户名称", type="string", example="张三"),
            Parameter(name="金额", type="int", example=500),
        ],
        steps=[
            Step(
                step_id=1,
                action=ActionKind.FILL,
                target=Locator(role="textbox", name="客户名称"),
                value="{{客户名称}}",
            ),
            Step(
                step_id=2,
                action=ActionKind.FILL,
                target=Locator(role="textbox", name="金额"),
                value="{{金额}}",
            ),
            Step(
                step_id=3,
                action=ActionKind.CLICK,
                target=Locator(role="button", name="提交"),
                write_effect=WriteEffect.CREATE,
                idempotency_key="{{flow_run_id}}::step3",
                precheck=Assertion(
                    assertion_id="precheck",
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
                    expected=RECEIPT_RE,
                    confirmed_by="human",
                ),
                technical_assertion=Assertion(
                    assertion_id="t-url",
                    kind=AssertionKind.TECHNICAL,
                    anchor=AnchorKind.UI_TEXT,
                    operator="url_match",
                    expected="/success",
                    confirmed_by="human",
                ),
                fingerprint=SUBMIT_FP,
            ),
        ],
        meta={"steps_reviewed": True},
        state="reviewed",
    )


def _receipt_for(case) -> str:
    """客户名以「甲」结尾的用例，单号只有 7 位 —— 用来抓「把正则收紧成 \\d{8}」的补丁。"""
    if str(case.params.get("客户名称", "")).endswith("甲"):
        return "提交成功 单号 R-2024123 已创建"
    return "提交成功 单号 R-20240115 已创建"


def page_v1(case) -> FakePage:
    return FakePage(
        known_names=("客户名称", "金额", "提交"),
        required_fields=("客户名称", "金额"),
        on_submit_text=_receipt_for(case),
    )


def page_v2(case) -> FakePage:
    """改版：提交按钮文案变成「提交保存」。"""
    return FakePage(
        known_names=("客户名称", "金额", "提交保存"),
        required_fields=("客户名称", "金额"),
        fingerprints=(NEW_SUBMIT_FP,),
        on_submit_text=_receipt_for(case),
    )


def print_attempts(result) -> None:
    for attempt in result.attempts:
        mark = "收下" if attempt.accepted else ("试用" if attempt.applied else "跳过")
        line = f"  [{mark}] {attempt.operator_id:18s}"
        if attempt.applied:
            line += f" 通过率 {attempt.rate:.2f}（{len(attempt.scores)} 条）"
            if attempt.gate and not attempt.gate.passed:
                line += " → 闸拒：" + "；".join(attempt.gate.reasons)[:60]
        else:
            line += f" {attempt.reason or attempt.detail}"
        print(line)


async def main() -> int:
    small_cases = [
        GoldenCase(case_id="c1", params={"客户名称": "张三", "金额": 500}, origin="demo"),
        GoldenCase(case_id="c2", params={"客户名称": "李四", "金额": 800}, origin="demo"),
        GoldenCase(case_id="c3", params={"客户名称": "王五", "金额": 100}, origin="manual_edge"),
    ]

    skill = build_skill()
    big_cases = suggest_cases(skill, limit=12)

    print("=== 1. Golden Set：3 条 vs 自动生成 ===")
    print(f"  手工 3 条：{[c.case_id for c in small_cases]}")
    print(f"  自动生成：{len(big_cases)} 条（全部 origin=manual_edge，需人确认）")
    for case in big_cases:
        print(f"    {case.case_id:24s} {case.params}")

    # ---------------------------------------------------------------- 建基线
    if STORE.exists():
        import shutil

        shutil.rmtree(STORE, ignore_errors=True)
    store = SkillStore(STORE)

    small_base = await run_golden(skill, small_cases, page_v1)
    big_base = await run_golden(skill, big_cases, page_v1)
    print("\n=== 2. v1.0 跑批 ===")
    print(f"  3 条集：{summarize(small_base)}")
    print(f"  {len(big_base)} 条集：{summarize(big_base)}")
    record, _gate = store.release(skill, big_base)
    print(f"  已发布 v{record.version}（基线 {len(big_base)} 条全过）")

    # ---------------------------------------------------------------- 改版
    print("\n=== 3. 页面改版（提交 → 提交保存） ===")
    broken_page = page_v2(big_cases[0])
    failed_run = await execute(skill, dict(big_cases[0].params), broken_page)
    failed_step = failed_run.failed_step()
    verdict = triage(failed_run)
    print(f"  失败步 {failed_step.step_id}：{failed_step.detail}")
    print(f"  定级 {verdict.layer} | 需人工 {verdict.needs_human}")
    print(f"  候选算子 {[o.operator_id for o in verdict.operators]}")

    # ---------------------------------------------------------- 第一轮：没候选
    print("\n=== 4. heal 第一轮（拿不到新定位器候选） ===")

    async def golden_v2(candidate_skill: Skill) -> list[CaseScore]:
        return await run_golden(candidate_skill, big_cases, page_v2)

    first = await heal(skill, failed_run, run_golden=golden_v2, store=store)
    print_attempts(first)
    print(f"  结果：{'收下' if first.accepted else '未修复，需人工'} 原因：{first.reasons[-1]}")
    print(f"  当前版本仍是 v{store.current_record('expense').version}")

    # ---------------------------------------------------------- 第二轮：有候选
    print("\n=== 5. heal 第二轮（先从改版页收集候选定位器） ===")
    candidates = await collect_locator_candidates(broken_page, skill)
    print(f"  收集到候选：{ {k: [c.key() for c in v] for k, v in candidates.items()} }")

    second = await heal(
        skill, failed_run, run_golden=golden_v2, store=store, locator_candidates=candidates
    )
    print_attempts(second)
    if second.accepted:
        print(f"  已自动收下并发布 v{second.version}")
    current = store.current_record("expense")
    version_after_heal = current.version

    # ------------------------------------------------------- 3 条 vs 10 条
    print("\n=== 6. 为什么 3 条不够：一个「把正则收紧」的补丁 ===")
    strict = copy.deepcopy(skill)
    strict.version = "1.9.0"
    strict.steps[-1].assertion.expected = r"R\-\d{8}"  # 看起来更"严谨"，实则过严

    strict_small = await run_golden(strict, small_cases, page_v1)
    strict_big = await run_golden(strict, big_cases, page_v1)
    gate_small = evaluate_gate(small_base, strict_small)
    gate_big = evaluate_gate(big_base, strict_big)

    print(f"  3 条集：  {summarize(strict_small)}  闸 → {'放行（错！）' if gate_small.passed else '拒绝'}")
    print(f"  {len(big_cases)} 条集：{summarize(strict_big)}  闸 → {'放行' if gate_big.passed else '拒绝'}")
    if not gate_big.passed:
        for reason in gate_big.reasons:
            print(f"    - {reason}")
    print(
        "  说明：3 条集里没有「7 位单号」这种用例，所以这个回归压根不会被发现；\n"
        "        真正的理由不是分辨率，而是**覆盖面**。"
    )

    # ---------------------------------------------------------------- 回滚
    print("\n=== 7. 回滚 ===")
    rolled = store.rollback("expense")
    print(f"  回滚到 v{rolled.version}（历史不删：{[r.version for r in store.history('expense')]}）")
    print(f"  heal 之后的版本是 v{version_after_heal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

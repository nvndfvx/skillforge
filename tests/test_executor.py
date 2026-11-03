"""离线测试：执行器 / 幂等门 / 断言求值 / 三层定位 / Golden 跑批。

全部用 FakePage，不需要浏览器：
    python tests/test_executor.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skillforge.executor.driver import execute, render  # noqa: E402
from skillforge.golden.runner import evaluate_gate, run_golden  # noqa: E402
from skillforge.healer.triage import triage  # noqa: E402
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
from skillforge.testing import DEFAULT_RECEIPT as RECEIPT  # noqa: E402
from skillforge.testing import FakePage  # noqa: E402

NEW_URL = "http://erp.local/expense/new"
OK_URL = "http://erp.local/expense/success"


# --------------------------------------------------------------------------- #
# 造技能
# --------------------------------------------------------------------------- #
def receipt_assertion(expected: str = r"R\-\d{6,}", aid: str = "a-entity") -> Assertion:
    return Assertion(
        assertion_id=aid,
        kind=AssertionKind.BUSINESS,
        anchor=AnchorKind.BUSINESS_ENTITY,
        operator="entity_exists",
        target=None,
        expected=expected,
        confirmed_by="human",
    )


def make_skill(
    *,
    precheck_expected: str | None = None,
    submit_target: Locator | None = None,
    submit_fp: ElementFingerprint | None = None,
    business_expected: str = r"R\-\d{6,}",
    technical_expected: str = "/expense/success",
) -> Skill:
    precheck = None
    if precheck_expected is not None:
        precheck = receipt_assertion(precheck_expected, "precheck")

    return Skill(
        skill_id="expense",
        name="报销单录入",
        parameters=[Parameter(name="客户名称", type="string")],
        steps=[
            Step(step_id=1, action=ActionKind.GOTO, target=Locator(), value=NEW_URL),
            Step(
                step_id=2,
                action=ActionKind.FILL,
                target=Locator(role="textbox", name="客户名称"),
                value="{{客户名称}}",
            ),
            Step(
                step_id=3,
                action=ActionKind.CLICK,
                target=submit_target or Locator(role="button", name="提交"),
                write_effect=WriteEffect.CREATE,
                idempotency_key="{{flow_run_id}}::step3",
                precheck=precheck,
                assertion=receipt_assertion(business_expected),
                technical_assertion=Assertion(
                    assertion_id="t-url",
                    kind=AssertionKind.TECHNICAL,
                    anchor=AnchorKind.UI_TEXT,
                    operator="url_match",
                    expected=technical_expected,
                    confirmed_by="human",
                ),
                fingerprint=submit_fp,
            ),
        ],
    )


def run(skill: Skill, params: dict, page: FakePage):
    return asyncio.run(execute(skill, params, page))


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_execute_success():
    page = FakePage()
    result = run(make_skill(precheck_expected=r"R-99999999"), {"客户名称": "张三"}, page)
    assert result.ok, result.to_dict()
    assert [s.status for s in result.steps] == ["ok", "ok", "ok"]
    assert ("fill", "客户名称", "张三") in page.calls
    assert ("click", "提交") in page.calls


def test_goto_step_does_not_need_locator():
    page = FakePage()
    result = run(make_skill(precheck_expected=r"R-99999999"), {"客户名称": "张三"}, page)
    assert result.steps[0].status == "ok"
    assert page.calls[0] == ("goto", NEW_URL)


# --------------------------------------------------------------------------- #
# 幂等门（最重要）
# --------------------------------------------------------------------------- #
def test_write_blocked_without_precheck():
    page = FakePage()
    result = run(make_skill(), {"客户名称": "张三"}, page)
    assert not result.ok
    blocked = result.steps[-1]
    assert blocked.status == "blocked"
    assert blocked.failure_layer == "precheck"
    # 关键：被拦住时一次点击都不能发生
    assert ("click", "提交") not in page.calls


def test_write_skipped_when_precheck_hits():
    page = FakePage(text=f"当前已有单据 {RECEIPT}")
    result = run(make_skill(precheck_expected=r"R-20240115"), {"客户名称": "张三"}, page)
    assert result.ok, result.to_dict()
    assert result.steps[-1].status == "skipped"
    assert ("click", "提交") not in page.calls, "precheck 命中还去点提交 = 重复提交"


def test_write_proceeds_when_precheck_misses():
    page = FakePage()
    result = run(make_skill(precheck_expected=r"R-99999999"), {"客户名称": "张三"}, page)
    assert result.steps[-1].status == "ok"
    assert ("click", "提交") in page.calls


# --------------------------------------------------------------------------- #
# 参数层
# --------------------------------------------------------------------------- #
def test_missing_param_fails_at_param_layer():
    page = FakePage()
    result = run(make_skill(precheck_expected=r"R-99999999"), {}, page)
    assert not result.ok
    failed = result.failed_step()
    assert failed.failure_layer == "param"
    assert "客户名称" in failed.detail


def test_render():
    assert render("{{ a }}", {"a": 1}) == "1"
    assert render("x{{客户名称}}y", {"客户名称": "张三"}) == "x张三y"
    assert render(None, {}) is None
    assert render(123, {}) == 123


# --------------------------------------------------------------------------- #
# 定位层 + 指纹兜底
# --------------------------------------------------------------------------- #
def test_locator_fail_without_fingerprint():
    page = FakePage()
    skill = make_skill(
        precheck_expected=r"R-99999999", submit_target=Locator(css="#submit-old")
    )
    result = run(skill, {"客户名称": "张三"}, page)
    failed = result.failed_step()
    assert failed.failure_layer == "locator"
    assert failed.healed_locator is False


def test_locator_healed_by_fingerprint():
    fp = ElementFingerprint(
        role="button",
        accessible_name="提交",
        neighbor_text="报销单",
        bbox_rel=(0.5, 0.9, 0.1, 0.05),
    )
    page = FakePage(fingerprints=(fp,))
    skill = make_skill(
        precheck_expected=r"R-99999999",
        submit_target=Locator(css="#submit-old"),
        submit_fp=fp,
    )
    result = run(skill, {"客户名称": "张三"}, page)
    assert result.ok, result.to_dict()
    assert result.steps[-1].healed_locator is True
    assert ("click", "提交") in page.calls


def test_write_step_requires_unique_locator():
    """写操作定位必须唯一：role+name 是子串匹配，「提交」可能命中「撤销提交」。"""

    class AmbiguousPage(FakePage):
        async def count(self, locator):
            return 2

    page = AmbiguousPage()
    result = run(make_skill(precheck_expected=r"R-99999999"), {"客户名称": "张三"}, page)
    failed = result.failed_step()
    assert failed.failure_layer == "locator", failed.failure_layer
    assert "不唯一" in failed.detail, failed.detail
    assert ("click", "提交") not in page.calls, "定位不唯一时一次点击都不能发生"


# --------------------------------------------------------------------------- #
# 断言层
# --------------------------------------------------------------------------- #
def test_business_assertion_failure():
    page = FakePage()
    skill = make_skill(precheck_expected=r"R-99999999", business_expected=r"R-11111111")
    result = run(skill, {"客户名称": "张三"}, page)
    failed = result.failed_step()
    assert failed.failure_layer == "assertion"
    assert failed.business_failures


def test_technical_failure_does_not_kill_skill():
    page = FakePage()
    skill = make_skill(precheck_expected=r"R-99999999", technical_expected="/nope")
    result = run(skill, {"客户名称": "张三"}, page)
    assert result.ok, "technical 断言只是环境探针，不该判技能死"
    assert result.steps[-1].technical_failures


# --------------------------------------------------------------------------- #
# 三层定位
# --------------------------------------------------------------------------- #
def test_triage_env_from_locator():
    page = FakePage()
    skill = make_skill(
        precheck_expected=r"R-99999999", submit_target=Locator(css="#submit-old")
    )
    t = triage(run(skill, {"客户名称": "张三"}, page))
    assert t.layer == "env"
    assert any(o.operator_id == "swap_locator" for o in t.operators)
    assert not t.needs_human


def test_triage_env_from_technical_only():
    page = FakePage()
    skill = make_skill(precheck_expected=r"R-99999999", technical_expected="/nope")
    result = run(skill, {"客户名称": "张三"}, page)
    t = triage(result)
    assert t.layer == "none", "technical 失败但步骤都成功，不该判为失败"

    # 人为把 technical 失败挂到一个 failed 步骤上，验证分流
    result.steps[-1].status = "failed"
    result.steps[-1].failure_layer = "step"
    t2 = triage(result)
    assert t2.layer == "env", t2.to_dict()


def test_triage_precheck():
    t = triage(run(make_skill(), {"客户名称": "张三"}, FakePage()))
    assert t.layer == "precheck"
    assert t.needs_human, "缺 precheck 只能人补，没有自动算子"
    assert any(o.operator_id == "author_precheck" for o in t.operators)


def test_triage_param():
    t = triage(run(make_skill(precheck_expected=r"R-99999999"), {}, FakePage()))
    assert t.layer == "param"
    assert any(o.operator_id == "coerce_param" for o in t.operators)


def test_triage_step_from_assertion():
    skill = make_skill(precheck_expected=r"R-99999999", business_expected=r"R-11111111")
    t = triage(run(skill, {"客户名称": "张三"}, FakePage()))
    assert t.layer == "step"
    assert t.operators


# --------------------------------------------------------------------------- #
# Golden Set 跑批 + 单调性闸
# --------------------------------------------------------------------------- #
def test_run_golden_scores_each_case():
    skill = make_skill(precheck_expected=r"R-99999999")
    cases = [
        GoldenCase(case_id="c1", params={"客户名称": "张三"}),
        GoldenCase(case_id="c2", params={"客户名称": "坏单"}),
        GoldenCase(case_id="c3", params={"客户名称": "李四"}),
    ]

    def factory(case: GoldenCase) -> FakePage:
        if case.params.get("客户名称") == "坏单":
            return FakePage(on_submit_text="提交失败，请重试")
        return FakePage()

    scores = asyncio.run(run_golden(skill, cases, factory))
    assert [s.case_id for s in scores] == ["c1", "c2", "c3"]
    assert [s.passed for s in scores] == [True, False, True]
    assert scores[1].detail


def test_gate_rejects_regression_end_to_end():
    old = [
        CaseScore("c1", True),
        CaseScore("c2", False),
        CaseScore("c3", True),
    ]
    new = [
        CaseScore("c1", True),
        CaseScore("c2", True),
        CaseScore("c3", False),  # 单点翻转
    ]
    gate = evaluate_gate(old, new)
    assert not gate.passed
    assert any("单点翻转" in r for r in gate.reasons)


def test_gate_accepts_improvement_end_to_end():
    old = [CaseScore("c1", True), CaseScore("c2", False)]
    new = [CaseScore("c1", True), CaseScore("c2", True)]
    gate = evaluate_gate(old, new)
    assert gate.passed, gate.reasons


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

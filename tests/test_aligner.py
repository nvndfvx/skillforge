"""离线测试：对齐器 / 单调性闸 / 幂等约束 / 断言分级。

不依赖浏览器与 playwright，纯 stdlib 可跑：
    python tests/test_aligner.py
也兼容 pytest：
    pytest tests/
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Windows CI 的 stdout 是 cp1252，本文件失败时会打印中文断言信息 ——
# 不强制 UTF-8 会 UnicodeEncodeError，把真正的失败盖成编码错误。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from skillforge.observer.aligner import align_actions  # noqa: E402
from skillforge.observer.recorder import infer_write_effect  # noqa: E402
from skillforge.schema.models import (  # noqa: E402
    Assertion,
    CaseScore,
    GoldenCase,
    Parameter,
    Skill,
    Step,
    extract_entity_candidates,
    monotonic_gate,
)
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    ElementFingerprint,
    Locator,
    RawAction,
    RawTrace,
    WriteEffect,
)


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
def act(
    idx: int,
    action: str,
    name: str = "",
    value: str | None = None,
    selector: str = "",
    role: str = "",
) -> RawAction:
    return RawAction(
        seq=idx, action=action, role=role, name=name, value=value, selector=selector
    )


def demo_a() -> list[RawAction]:
    return [
        act(0, "goto", "", role="document"),
        act(1, "fill", "客户名称", "张三", "#customer", role="textbox"),
        act(2, "fill", "金额", "500", "#amount", role="textbox"),
        act(3, "click", "提交", None, "#submit", role="button"),
    ]


def demo_b() -> list[RawAction]:
    return [
        act(0, "goto", "", role="document"),
        act(1, "fill", "客户名称", "李四", "#customer", role="textbox"),
        act(2, "fill", "金额", "800", "#amount", role="textbox"),
        act(3, "click", "提交", None, "#submit", role="button"),
    ]


# --------------------------------------------------------------------------- #
# 对齐器
# --------------------------------------------------------------------------- #
def test_two_demos_yield_variables():
    report = align_actions(demo_a(), demo_b())
    labels = sorted(v.label for v in report.variables)
    assert labels == ["客户名称", "金额"], labels
    assert report.unstable == [], report.unstable
    assert report.extra_in_a == [] and report.extra_in_b == []
    for v in report.variables:
        assert v.source == "aligned_diff"
    # 变量必须能生成参数名
    assert {v.suggested_name for v in report.variables} == {"客户名称", "金额"}


def test_identical_demos_warn_no_variables():
    report = align_actions(demo_a(), demo_a())
    assert report.variables == []
    assert any("未产生任何变量" in w for w in report.warnings), report.warnings


def test_target_change_marks_unstable():
    b = demo_b()
    b[3] = act(3, "click", "提交", None, "#submit-v2", role="button")
    report = align_actions(demo_a(), b)
    assert len(report.unstable) == 1
    assert report.unstable[0].selector_a == "#submit"
    assert report.unstable[0].selector_b == "#submit-v2"


def test_extra_step_in_b():
    b = demo_b() + [act(4, "click", "确认", None, "#confirm", role="button")]
    report = align_actions(demo_a(), b)
    assert report.extra_in_b == [4], report.extra_in_b
    assert any("未对齐步骤" in w for w in report.warnings)


def test_empty_demo_is_error():
    report = align_actions([], [])
    assert not report.ok
    assert any("没有任何可对齐" in e for e in report.errors)


# --------------------------------------------------------------------------- #
# 单调性闸
# --------------------------------------------------------------------------- #
def score(case_id: str, passed: bool) -> CaseScore:
    return CaseScore(case_id=case_id, passed=passed)


def test_gate_accepts_improvement():
    old = [score("c1", True), score("c2", True), score("c3", False)]
    new = [score("c1", True), score("c2", True), score("c3", True)]
    result = monotonic_gate(old, new)
    assert result.passed, result.reasons
    assert result.new_rate > result.old_rate


def test_gate_rejects_single_point_flip():
    old = [score("c1", True), score("c2", True), score("c3", False)]
    new = [score("c1", True), score("c2", False), score("c3", True)]
    result = monotonic_gate(old, new)
    assert not result.passed
    assert any("单点翻转" in r for r in result.reasons), result.reasons


def test_gate_rejects_missing_case():
    old = [score("c1", True), score("c2", True)]
    new = [score("c1", True)]
    result = monotonic_gate(old, new)
    assert not result.passed
    assert any("缺项" in r for r in result.reasons), result.reasons


# --------------------------------------------------------------------------- #
# 断言分级 + 锚点
# --------------------------------------------------------------------------- #
def test_business_assertion_must_anchor_entity():
    bad = Assertion(
        assertion_id="a1",
        kind=AssertionKind.BUSINESS,
        anchor=AnchorKind.UI_TEXT,
        operator="contains",
        expected="提交成功",
    )
    assert any("ui_text" in e for e in bad.errors())

    good = Assertion(
        assertion_id="a2",
        kind=AssertionKind.BUSINESS,
        anchor=AnchorKind.BUSINESS_ENTITY,
        operator="entity_exists",
        expected=r"R-\d{6,}",
        confirmed_by="human",
    )
    assert good.errors() == []
    assert good.is_trusted


def test_unconfirmed_assertion_is_not_trusted():
    a = Assertion(
        assertion_id="a3",
        kind=AssertionKind.BUSINESS,
        anchor=AnchorKind.BUSINESS_ENTITY,
        operator="entity_exists",
        expected="R-123456",
    )
    assert not a.is_trusted
    assert any("未经人确认" in w for w in a.warnings())


def test_technical_assertion_may_anchor_ui():
    a = Assertion(
        assertion_id="t1",
        kind=AssertionKind.TECHNICAL,
        anchor=AnchorKind.UI_TEXT,
        operator="url_match",
        expected="/success",
    )
    assert a.errors() == []


# --------------------------------------------------------------------------- #
# 幂等硬约束
# --------------------------------------------------------------------------- #
def test_write_step_requires_idempotency_and_precheck():
    step = Step(
        step_id=1,
        action=ActionKind.SUBMIT,
        target=Locator(role="button", name="提交"),
        write_effect=WriteEffect.CREATE,
    )
    errs = step.errors()
    assert any("idempotency_key" in e for e in errs), errs
    assert any("precheck" in e for e in errs), errs


def test_write_step_ok_when_guarded():
    precheck = Assertion(
        assertion_id="p1",
        kind=AssertionKind.TECHNICAL,
        anchor=AnchorKind.UI_TEXT,
        operator="entity_exists",
        expected="R-123456",
        confirmed_by="human",
    )
    step = Step(
        step_id=1,
        action=ActionKind.SUBMIT,
        target=Locator(role="button", name="提交"),
        write_effect=WriteEffect.CREATE,
        idempotency_key="{{flow_run_id}}::{{record_id}}",
        precheck=precheck,
    )
    assert step.errors() == []


def test_skill_draft_can_exist_but_cannot_publish():
    """生命周期修复的回归测试：编译产物天然是 draft，不能被严格校验挡住保存。"""
    business = Assertion(
        assertion_id="a4",
        kind=AssertionKind.BUSINESS,
        anchor=AnchorKind.BUSINESS_ENTITY,
        operator="entity_exists",
        expected=r"R-\d{6,}",
        confirmed_by="llm_proposed",  # 还没人审
    )
    step = Step(
        step_id=1,
        action=ActionKind.SUBMIT,
        target=Locator(role="button", name="提交"),
        assertion=business,
    )
    skill = Skill(
        skill_id="s1",
        name="录单",
        parameters=[Parameter(name="客户名称", type="string")],
        steps=[step],
    )
    # draft 允许结构上存下来
    assert skill.errors() == [], skill.errors()
    # 但发布被卡住：没人审 + 没有可信 business 断言
    blockers = skill.publish_blockers()
    assert any("未经人审" in b for b in blockers), blockers
    assert any("business 断言" in b for b in blockers), blockers

    # 人审之后放行：三道勾缺一不可
    business.confirmed_by = "human"
    skill.state = "reviewed"

    # 第三道勾（步骤顺序）没打 → 仍然不放行
    assert any("步骤顺序" in b for b in skill.publish_blockers())
    checklist = skill.review_checklist()
    assert checklist["① 步骤顺序已人审"] is False
    assert checklist["② 断言已人审"] is True

    skill.meta["steps_reviewed"] = True
    assert all(skill.review_checklist().values()), skill.review_checklist()
    assert skill.publish_blockers() == [], skill.publish_blockers()


def test_unknown_write_effect_defaults_safe():
    """判定不出写副作用时默认 UNKNOWN（按写操作处理），不是 NONE。"""
    assert infer_write_effect("click", "某个看不出含义的按钮") is WriteEffect.UNKNOWN
    assert infer_write_effect("click", "") is WriteEffect.UNKNOWN
    # 明确的只读动作仍然放行
    assert infer_write_effect("click", "查询") is WriteEffect.NONE


def test_unknown_write_effect_is_treated_as_write():
    precheck = Assertion(
        assertion_id="p2",
        kind=AssertionKind.TECHNICAL,
        anchor=AnchorKind.UI_TEXT,
        operator="url_match",
        expected="/list",
        confirmed_by="human",
    )
    step = Step(
        step_id=1,
        action=ActionKind.CLICK,
        target=Locator(role="button", name="某个看不出含义的按钮"),
        write_effect=WriteEffect.UNKNOWN,
        idempotency_key="k1",
        precheck=precheck,
    )
    # UNKNOWN 也算写操作：已经带了幂等键 + precheck，所以无硬错误
    assert step.errors() == [], step.errors()
    assert step.is_write
    assert any("unknown" in w for w in step.warnings())


def test_fingerprint_drift_marks_unstable():
    """定位器不稳定判定必须能用指纹，而不是只能靠 selector。"""
    a = demo_a()
    b = demo_b()
    a[3].fingerprint = ElementFingerprint(
        role="button",
        accessible_name="提交",
        neighbor_text="报销单",
        bbox_rel=(0.5, 0.9, 0.1, 0.05),
    )
    b[3].fingerprint = ElementFingerprint(
        role="textbox",
        accessible_name="金额",
        neighbor_text="发票",
        bbox_rel=(0.1, 0.2, 0.3, 0.1),
    )
    report = align_actions(a, b)
    assert len(report.unstable) == 1, report.unstable


# --------------------------------------------------------------------------- #
# 业务实体候选（锚点⑤ 的自动发现）
# --------------------------------------------------------------------------- #
def test_entity_candidates():
    before = "报销单填写中"
    after = "提交成功 单号 R-20240115 已创建"
    cands = extract_entity_candidates(before, after)
    assert "R-20240115" in cands, cands


def test_entity_candidates_ignores_existing():
    before = "单号 R-20240115"
    after = "单号 R-20240115"
    assert extract_entity_candidates(before, after) == []


# --------------------------------------------------------------------------- #
# 元素指纹
# --------------------------------------------------------------------------- #
def test_fingerprint_identical_scores_high():
    fp = ElementFingerprint(
        role="button",
        accessible_name="提交",
        text_norm="提交",
        neighbor_text="报销单",
        bbox_rel=(0.1, 0.2, 0.3, 0.4),
    )
    assert fp.similarity(fp) == 1.0


def test_fingerprint_different_scores_low():
    a = ElementFingerprint(role="button", accessible_name="提交", neighbor_text="报销单")
    b = ElementFingerprint(role="textbox", accessible_name="金额", neighbor_text="发票")
    assert a.similarity(b) < 0.35


# --------------------------------------------------------------------------- #
# 写副作用推断
# --------------------------------------------------------------------------- #
def test_infer_write_effect():
    assert infer_write_effect("click", "提交") is WriteEffect.CREATE
    assert infer_write_effect("click", "保存") is WriteEffect.CREATE
    assert infer_write_effect("click", "删除") is WriteEffect.DELETE
    assert infer_write_effect("click", "修改") is WriteEffect.UPDATE
    assert infer_write_effect("click", "取消") is WriteEffect.NONE
    assert infer_write_effect("fill", "提交") is WriteEffect.NONE


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def main() -> int:
    tests = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_")]
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

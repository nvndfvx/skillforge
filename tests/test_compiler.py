"""离线测试：表单快照补录（H4）/ 蒸馏器 / 实体正则。

不依赖浏览器：
    python tests/test_compiler.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skillforge.compiler.distiller import distill, entity_regex, infer_type  # noqa: E402
from skillforge.observer.aligner import align_actions  # noqa: E402
from skillforge.observer.reconcile import (  # noqa: E402
    FormField,
    merge_synthesized,
    reconcile_form_snapshots,
)
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    ElementFingerprint,
    RawAction,
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
    role: str = "",
    url: str = "",
    before: str = "",
    after: str = "",
    pause: int = 0,
) -> RawAction:
    return RawAction(
        seq=idx,
        action=action,
        role=role,
        name=name,
        value=value,
        url=url,
        before_text=before,
        after_text=after,
        pause_before_ms=pause,
    )


def demo(customer: str, amount: str) -> list[RawAction]:
    base = "http://erp.local/expense/new"
    return [
        act(0, "goto", "", role="document", url=base),
        act(1, "fill", "客户名称", customer, role="textbox", url=base),
        act(2, "fill", "金额", amount, role="textbox", url=base, pause=4200),
        act(
            3,
            "click",
            "提交",
            None,
            role="button",
            url="http://erp.local/expense/success",
            before="报销单填写中",
            after="提交成功 单号 R-20240115 已创建",
        ),
    ]


# --------------------------------------------------------------------------- #
# H4：表单快照补录
# --------------------------------------------------------------------------- #
def test_snapshot_fills_missing_field():
    recorded = [act(0, "fill", "备注", "加急", role="textbox")]
    snapshots = [
        [
            FormField(role="textbox", accessible_name="客户名称", value="张三"),
            FormField(role="textbox", accessible_name="备注", value="加急"),
        ]
    ]
    result = reconcile_form_snapshots(recorded, snapshots)
    assert result.total == 1, result.to_dict()
    synth = result.synthesized[0][0]
    assert synth.name == "客户名称"
    assert synth.value == "张三"
    assert synth.source == "snapshot_diff"


def test_snapshot_skips_covered_field():
    recorded = [act(0, "fill", "客户名称", "张三", role="textbox")]
    snapshots = [[FormField(role="textbox", accessible_name="客户名称", value="张三")]]
    result = reconcile_form_snapshots(recorded, snapshots)
    assert result.total == 0, result.to_dict()


def test_snapshot_respects_baseline_prefill():
    """页面自带的预填值不能被误判成用户填的。"""
    baseline = [FormField(role="textbox", accessible_name="制单人", value="系统")]
    recorded = [act(0, "fill", "客户名称", "张三", role="textbox")]
    snapshots = [
        [
            FormField(role="textbox", accessible_name="制单人", value="系统"),
            FormField(role="textbox", accessible_name="客户名称", value="张三"),
        ]
    ]
    result = reconcile_form_snapshots(recorded, snapshots, baseline=baseline)
    assert result.total == 0, result.to_dict()


def test_snapshot_catches_second_change():
    """日期控件改了两次，第二次没触发 change，也要补上。"""
    recorded = [act(0, "fill", "客户名称", "张三", role="textbox")]
    snapshots = [
        [FormField(role="textbox", accessible_name="日期", value="2024-01-01")],
        [FormField(role="textbox", accessible_name="日期", value="2024-02-02")],
    ]
    result = reconcile_form_snapshots(recorded, snapshots)
    assert result.total == 2, result.to_dict()


def test_merge_renumbers_seq():
    recorded = [act(0, "fill", "客户名称", "张三"), act(1, "click", "提交")]
    snapshots = [
        [],
        [FormField(role="textbox", accessible_name="日期", value="2024-01-01")],
    ]
    result = reconcile_form_snapshots(recorded, snapshots)
    merged = merge_synthesized(recorded, result)
    assert [a.seq for a in merged] == list(range(len(merged)))
    assert any(a.source == "snapshot_diff" for a in merged)


# --------------------------------------------------------------------------- #
# 纯函数小工具
# --------------------------------------------------------------------------- #
def test_entity_regex():
    # 下界而不是精确位数：单号变长时不会变成假阴性
    assert entity_regex("R-20240115") == r"R\-\d{6,}"
    assert entity_regex("20240115") == r"\d{6,}"
    # 短号码也保底 4 位，避免过宽的 \d+
    assert entity_regex("A-2024") == r"A\-\d{4,}"


def test_infer_type():
    assert infer_type(["500", "800"]) == "int"
    assert infer_type(["2024-01-01", "2024-02-02"]) == "date"
    assert infer_type(["张三", "李四"]) == "string"


# --------------------------------------------------------------------------- #
# 蒸馏器
# --------------------------------------------------------------------------- #
def _distill_demo():
    a, b = demo("张三", "500"), demo("李四", "800")
    report = align_actions(a, b)
    return distill(report, a, skill_id="expense-create", name="报销单录入"), report


def test_distill_produces_parameters_and_steps():
    result, report = _distill_demo()
    skill = result.skill
    names = [p.name for p in skill.parameters]
    assert names == ["客户名称", "金额"], names
    assert len(skill.steps) == 4
    assert skill.state == "draft"
    # 变量被模板化
    assert skill.steps[1].value == "{{客户名称}}"
    assert skill.steps[2].value == "{{金额}}"
    # goto 的目标 URL 落在 value 上（Step 没有独立 url 字段）
    assert skill.steps[0].value == "http://erp.local/expense/new"


def test_distill_param_types():
    result, _ = _distill_demo()
    by_name = {p.name: p for p in result.skill.parameters}
    assert by_name["金额"].type == "int"
    assert by_name["客户名称"].type == "string"


def test_distill_marks_write_effect_and_idempotency():
    result, _ = _distill_demo()
    submit = result.skill.steps[-1]
    assert submit.write_effect is WriteEffect.CREATE
    assert submit.idempotency_key, "写操作必须自动生成幂等键占位"
    assert submit.precheck is None
    # 缺 precheck 必须报出来
    assert any("precheck" in e for e in result.errors), result.errors
    assert any("precheck" in w for w in result.warnings), result.warnings


def test_distill_generates_business_assertion_from_receipt():
    result, _ = _distill_demo()
    assertion = result.skill.steps[-1].assertion
    assert assertion is not None
    assert assertion.kind is AssertionKind.BUSINESS
    assert assertion.anchor is AnchorKind.BUSINESS_ENTITY
    assert assertion.expected == r"R\-\d{6,}"
    # 编译器只提议，不确认
    assert assertion.confirmed_by == "llm_proposed"
    assert not assertion.is_trusted


def test_distill_generates_technical_probe_on_url_change():
    result, _ = _distill_demo()
    tech = result.skill.steps[-1].technical_assertion
    assert tech is not None
    assert tech.kind is AssertionKind.TECHNICAL
    assert tech.operator == "url_match"


def test_distill_records_review_priority_by_pause():
    result, _ = _distill_demo()
    priority = result.skill.meta["review_priority"]
    assert any(item["step_id"] == 3 for item in priority), priority


def test_distill_warns_when_no_variables():
    a = demo("张三", "500")
    report = align_actions(a, a)
    result = distill(report, a, skill_id="s")
    assert any("没有产出任何变量" in w for w in result.warnings), result.warnings
    assert result.skill.parameters == []


def test_distill_marks_fragile_from_fingerprint_drift():
    a, b = demo("张三", "500"), demo("李四", "800")
    a[3].fingerprint = ElementFingerprint(
        role="button", accessible_name="提交", neighbor_text="报销单",
        bbox_rel=(0.5, 0.9, 0.1, 0.05),
    )
    b[3].fingerprint = ElementFingerprint(
        role="textbox", accessible_name="金额", neighbor_text="发票",
        bbox_rel=(0.1, 0.2, 0.3, 0.1),
    )
    report = align_actions(a, b)
    result = distill(report, a, skill_id="s")
    assert any(s.fragile for s in result.skill.steps), "指纹漂移必须标 fragile"
    assert result.skill.meta["fragile_step_ids"]


def test_goto_step_carries_url_and_needs_no_locator():
    """goto 不需要元素定位器，但必须带目标 URL。

    这条是跑示例时发现的：Step 没有独立 url 字段，goto 的目标只能放 value，
    而校验又要求 target —— 结果是每个 goto 都被误报「缺定位器」。
    """
    result, _ = _distill_demo()
    goto = result.skill.steps[0]
    assert goto.action is ActionKind.GOTO
    assert goto.value == "http://erp.local/expense/new"
    assert goto.target.is_empty()
    assert goto.errors() == [], goto.errors()


def test_distill_draft_is_not_publishable():
    result, _ = _distill_demo()
    blockers = result.skill.publish_blockers()
    assert blockers, "未人审的草稿不得发布"
    assert any("未经人审" in b for b in blockers), blockers


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

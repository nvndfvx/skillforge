"""离线测试：多技能编排（引用解析 / 向前引用校验 / 失败中止 / 产出传递）。

    python tests/test_flow.py
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from skillforge.flow import (  # noqa: E402
    Flow,
    FlowStep,
    MissingRef,
    extract_outputs,
    resolve_refs,
    run_flow,
)
from skillforge.schema.models import Assertion, Parameter, Precondition, Skill, Step  # noqa: E402
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    Locator,
    WriteEffect,
)
from skillforge.testing import DEFAULT_RECEIPT, FakePage  # noqa: E402


def make_skill(skill_id: str, *, receipt: str = DEFAULT_RECEIPT) -> Skill:
    """一个"填客户名 → 提交"的小技能，用来当流程里的节点。"""
    return Skill(
        skill_id=skill_id,
        name=skill_id,
        parameters=[Parameter(name="客户名称", type="string")],
        preconditions=[Precondition(kind="on_page", locator_or_path="http://erp.local/new")],
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


# --------------------------------------------------------------------------- #
# 引用解析
# --------------------------------------------------------------------------- #
def test_resolve_refs_basic():
    ctx = {"params": {"单据": "X1"}, "flow_run_id": "run-9", "steps": {"a": {"entity": "R-20240115"}}}
    assert resolve_refs("{{steps.a.entity}}", ctx) == "R-20240115"
    assert resolve_refs("单号：{{steps.a.entity}}", ctx) == "单号：R-20240115"
    assert resolve_refs({"p": "{{params.单据}}"}, ctx) == {"p": "X1"}
    assert resolve_refs(["{{flow_run_id}}", 1], ctx) == ["run-9", 1]


def test_resolve_refs_preserves_type_on_whole_match():
    ctx = {"params": {"数量": 3, "加急": True}}
    assert resolve_refs("{{params.数量}}", ctx) == 3, "整串是引用时要保留数字类型"
    assert resolve_refs("{{params.加急}}", ctx) is True
    assert resolve_refs("数量={{params.数量}}", ctx) == "数量=3"


def test_resolve_refs_missing_raises():
    for bad in ("{{steps.x.entity}}", "{{params.没这个}}"):
        try:
            resolve_refs(bad, {"params": {}, "steps": {}})
        except MissingRef:
            pass
        else:
            raise AssertionError(f"应该报 MissingRef：{bad}")


# --------------------------------------------------------------------------- #
# 静态校验
# --------------------------------------------------------------------------- #
def _flow(*steps) -> Flow:
    return Flow(flow_id="purchase", name="采购", steps=list(steps))


def test_validate_ok():
    skills = {"query": make_skill("query"), "order": make_skill("order")}
    flow = _flow(
        FlowStep("q", "query", {"客户名称": "张三"}),
        FlowStep("o", "order", {"客户名称": "{{steps.q.entity}}"}),
    )
    assert flow.validate(skills) == []


def test_validate_forward_reference_is_rejected():
    """只能向前引用：引后面的步骤等于写一个永远拿不到数据的编排。"""
    skills = {"query": make_skill("query"), "order": make_skill("order")}
    flow = _flow(
        FlowStep("q", "query", {"客户名称": "{{steps.o.entity}}"}),
        FlowStep("o", "order", {"客户名称": "张三"}),
    )
    problems = flow.validate(skills)
    assert any("不是更早的步骤" in p for p in problems), problems


def test_validate_self_reference_is_rejected():
    skills = {"query": make_skill("query")}
    flow = _flow(FlowStep("q", "query", {"客户名称": "{{steps.q.entity}}"}))
    assert any("不是更早的步骤" in p for p in flow.validate(skills))


def test_validate_unknown_skill_and_duplicate_step_id():
    skills = {"query": make_skill("query")}
    flow = _flow(
        FlowStep("a", "query", {"客户名称": "x"}),
        FlowStep("a", "query", {"客户名称": "y"}),
        FlowStep("b", "nope", {}),
    )
    problems = flow.validate(skills)
    assert any("step_id 重复" in p for p in problems), problems
    assert any("不存在的技能" in p for p in problems), problems


def test_validate_undeclared_flow_param_and_unknown_ref():
    skills = {"query": make_skill("query")}
    flow = _flow(FlowStep("a", "query", {"客户名称": "{{params.没定义}}"}))
    assert any("没定义" in p for p in flow.validate(skills))

    flow2 = _flow(FlowStep("a", "query", {"客户名称": "{{乱写}}"}))
    assert any("不认识这个引用" in p for p in flow2.validate(skills))


def test_validate_missing_required_param():
    skills = {"query": make_skill("query")}
    flow = _flow(FlowStep("a", "query", {}))
    assert any("缺必填参数" in p for p in flow.validate(skills))


def test_flow_from_dict_rejects_empty_steps():
    try:
        Flow.from_dict({"flow_id": "x", "steps": []})
    except ValueError:
        pass
    else:
        raise AssertionError("空 steps 应该被拒")


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def test_flow_passes_entity_forward():
    """第一步的产出（单号）当第二步的入参 —— 这是编排唯一的价值。"""
    skills = {"query": make_skill("query"), "order": make_skill("order")}
    flow = _flow(
        FlowStep("q", "query", {"客户名称": "张三"}),
        FlowStep("o", "order", {"客户名称": "{{steps.q.entity}}"}),
    )
    pages = []

    def factory(step):
        page = FakePage(required_fields=("客户名称",))
        pages.append(page)
        return page

    result = asyncio.run(run_flow(flow, skills, factory))
    assert result.ok, result.to_dict()
    assert [s.status for s in result.steps] == ["ok", "ok"]
    assert result.steps[0].outputs["entity"].startswith("R-")
    # 第二步真正拿到的入参必须等于第一步的单号
    assert pages[1].fields["客户名称"].startswith("R-"), pages[1].fields


def test_flow_aborts_after_failure_by_default():
    """默认中止：后面依赖前一步产出，硬跑只会拿空参数做错事。"""
    skills = {"query": make_skill("query"), "order": make_skill("order")}
    flow = _flow(
        FlowStep("q", "query", {"客户名称": "张三"}),
        FlowStep("o", "order", {"客户名称": "{{steps.q.entity}}"}),
        FlowStep("n", "order", {"客户名称": "李四"}),
    )

    def factory(step):
        return FakePage(on_submit_text="提交失败：服务繁忙", required_fields=("客户名称",))

    result = asyncio.run(run_flow(flow, skills, factory))
    assert not result.ok
    assert result.aborted_at == "q"
    assert [s.status for s in result.steps] == ["failed", "skipped", "skipped"]
    assert "中止" in result.steps[1].detail


def test_optional_step_failure_does_not_abort():
    skills = {"query": make_skill("query"), "order": make_skill("order")}
    flow = _flow(
        FlowStep("q", "query", {"客户名称": "张三"}, optional=True),
        FlowStep("o", "order", {"客户名称": "李四"}),
    )
    calls = {"n": 0}

    def factory(step):
        calls["n"] += 1
        bad = calls["n"] == 1
        return FakePage(
            on_submit_text="提交失败：服务繁忙" if bad else DEFAULT_RECEIPT,
            required_fields=("客户名称",),
        )

    result = asyncio.run(run_flow(flow, skills, factory))
    assert [s.status for s in result.steps] == ["failed", "ok"]
    assert not result.ok, "optional 失败不算中止，但整条流仍然不算成功"
    assert result.aborted_at == ""


def test_missing_ref_at_runtime_fails_that_step():
    """参数引用解析不出来 = 参数错误，不能带病执行。"""
    skills = {"order": make_skill("order")}
    flow = _flow(FlowStep("o", "order", {"客户名称": "{{steps.nothere.entity}}"}))
    called = {"n": 0}

    def factory(step):
        called["n"] += 1
        return FakePage()

    result = asyncio.run(run_flow(flow, skills, factory))
    assert result.steps[0].failure_layer == "param"
    assert called["n"] == 0, "解析失败就不该去开页面"
    assert not result.ok


def test_start_url_override_does_not_mutate_shared_skill():
    skill = make_skill("order")
    skills = {"order": skill}
    flow = _flow(FlowStep("o", "order", {"客户名称": "张三"}, start_url="http://other/x"))

    def factory(step):
        return FakePage(required_fields=("客户名称",))

    asyncio.run(run_flow(flow, skills, factory))
    assert skill.preconditions[0].locator_or_path == "http://erp.local/new", "不能污染复用的技能对象"


def test_extract_outputs_uses_business_anchor():
    skill = make_skill("query")
    outputs = extract_outputs(skill, "提交成功 单号 R-20240115 已创建", {})
    assert outputs["entity"] == "R-20240115"
    assert outputs["entity_step"] == 2
    assert "entity" not in extract_outputs(skill, "没有单号", {})


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

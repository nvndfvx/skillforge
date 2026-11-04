"""离线测试：Healer 自动打补丁（改一版 → 跑闸 → 被拒/被收）+ Golden 用例生成。

    python tests/test_healer.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import temp_dir  # noqa: E402

from skillforge.executor.candidates import collect_locator_candidates  # noqa: E402
from skillforge.executor.driver import execute  # noqa: E402
from skillforge.golden.suggest import suggest_cases  # noqa: E402
from skillforge.healer.patcher import (  # noqa: E402
    _op_drop_optional_step,
    _op_add_wait,
    HealContext,
    heal,
)
from skillforge.healer.triage import triage  # noqa: E402
from skillforge.registry import SkillStore  # noqa: E402
from skillforge.schema.models import (  # noqa: E402
    Assertion,
    CaseScore,
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
NEW_URL = "http://erp.local/expense/new"
SUBMIT_FP = ElementFingerprint(
    role="button", accessible_name="提交", neighbor_text="报销单", bbox_rel=(0.5, 0.9, 0.1, 0.05)
)


def receipt_assertion(expected: str = RECEIPT_RE, aid: str = "a-entity") -> Assertion:
    return Assertion(
        assertion_id=aid,
        kind=AssertionKind.BUSINESS,
        anchor=AnchorKind.BUSINESS_ENTITY,
        operator="entity_exists",
        expected=expected,
        confirmed_by="human",
    )


def make_skill(*, with_precheck: bool = True, submit_target: Locator | None = None) -> Skill:
    precheck = receipt_assertion(r"R-99999999", "precheck") if with_precheck else None
    return Skill(
        skill_id="expense",
        name="报销单录入",
        version="1.0.0",
        parameters=[Parameter(name="客户名称", type="string")],
        steps=[
            Step(step_id=1, action=ActionKind.FILL,
                 target=Locator(role="textbox", name="客户名称"), value="{{客户名称}}"),
            Step(step_id=2, action=ActionKind.CLICK,
                 # 默认用一个「页面上已经不存在」的旧 css 定位器，
                 # 这样才需要 swap_locator 换成一个真正能命中的新定位器。
                 target=submit_target or Locator(css="#submit-old"),
                 write_effect=WriteEffect.CREATE,
                 idempotency_key="{{flow_run_id}}::step2",
                 precheck=precheck,
                 assertion=receipt_assertion(),
                 technical_assertion=Assertion(
                     assertion_id="t-url", kind=AssertionKind.TECHNICAL,
                     anchor=AnchorKind.UI_TEXT, operator="url_match",
                     expected="/success", confirmed_by="human"),
                 fingerprint=SUBMIT_FP),
        ],
    )


async def _failing_run(skill: Skill):
    """提交按钮的旧定位器在页面上不存在 → locator 层失败。"""
    page = FakePage(known_names=("客户名称", "提交"))
    return await execute(skill, {"客户名称": "张三"}, page)


async def _ok_runner(skill: Skill) -> list[CaseScore]:
    return [CaseScore("c1", True), CaseScore("c2", True)]


async def _fail_runner(skill: Skill) -> list[CaseScore]:
    return [CaseScore("c1", True), CaseScore("c2", False)]


# --------------------------------------------------------------------------- #
# 自动打补丁
# --------------------------------------------------------------------------- #
def test_heal_accepts_locator_patch():
    skill = make_skill()
    run = asyncio.run(_failing_run(skill))
    assert run.failed_step().failure_layer == "locator"

    result = asyncio.run(
        heal(
            skill,
            run,
            run_golden=_ok_runner,
            baseline_scores=[CaseScore("c1", True), CaseScore("c2", True)],
            locator_candidates={2: [Locator(role="button", name="提交")]},
        )
    )
    assert result.accepted, result.to_dict()
    assert result.attempts[0].operator_id == "swap_locator"
    assert result.attempts[0].accepted
    assert result.skill.steps[-1].target.name == "提交"
    assert result.skill.meta["patched_by"] == "swap_locator"


def test_heal_writes_new_version_to_store():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        skill = make_skill()
        store.release(skill, [CaseScore("c1", True), CaseScore("c2", True)])
        assert store.current_record("expense").version == "1.0.0"

        run = asyncio.run(_failing_run(skill))
        result = asyncio.run(
            heal(
                skill,
                run,
                run_golden=_ok_runner,
                store=store,
                locator_candidates={2: [Locator(role="button", name="提交")]},
            )
        )
        assert result.accepted, result.to_dict()
        assert result.version == "1.1.0"
        assert store.current_record("expense").version == "1.1.0"
        assert store.load_skill("expense").meta["patched_by"] == "swap_locator"


def test_heal_rejects_patch_that_regresses():
    """补丁没过闸 → 不入库、换下一个算子，最后判需要人工。"""
    skill = make_skill()
    run = asyncio.run(_failing_run(skill))

    result = asyncio.run(
        heal(
            skill,
            run,
            run_golden=_fail_runner,  # 每个候选补丁都让 c2 挂掉
            baseline_scores=[CaseScore("c1", True), CaseScore("c2", True)],
            locator_candidates={2: [Locator(role="button", name="提交")]},
        )
    )
    assert not result.accepted
    assert result.needs_human
    assert result.skill is None

    tried = [a for a in result.attempts if a.applied]
    assert tried, "至少应该真正试过一个算子"
    for attempt in tried:
        assert not attempt.accepted
        assert attempt.gate is not None and not attempt.gate.passed

    # swap_locator 被闸拒之后，应该继续试 add_wait
    assert any(a.operator_id == "add_wait" and a.applied for a in result.attempts)


def test_heal_needs_human_on_precheck_layer():
    """缺 precheck 是写操作被拦，不是技术故障 —— 没有自动算子，立刻转人工。"""
    skill = make_skill(with_precheck=False)
    page = FakePage()
    run = asyncio.run(execute(skill, {"客户名称": "张三"}, page))
    assert run.failed_step().failure_layer == "precheck"

    result = asyncio.run(
        heal(skill, run, run_golden=_ok_runner, baseline_scores=[CaseScore("c1", True)])
    )
    assert result.needs_human
    assert not result.attempts, "不该尝试任何自动补丁"


def test_heal_requires_gate_input():
    skill = make_skill()
    run = asyncio.run(_failing_run(skill))
    try:
        asyncio.run(heal(skill, run, run_golden=_ok_runner))
    except ValueError as exc:
        assert "基线" in str(exc) or "store" in str(exc)
    else:
        raise AssertionError("没有闸的基线就不该跑")


def test_add_wait_operator_bumps_timeout():
    skill = make_skill()
    ctx = HealContext(failed_step=type("S", (), {"step_id": 2})())
    patched, detail = _op_add_wait(skill, ctx)
    assert patched is not None, detail
    assert patched.steps[-1].assertion.timeout_ms == 15000
    # 原技能不能被改
    assert skill.steps[-1].assertion.timeout_ms == 5000


def test_drop_step_refuses_write_step():
    skill = make_skill()
    ctx = HealContext(failed_step=type("S", (), {"step_id": 2})())
    patched, detail = _op_drop_optional_step(skill, ctx)
    assert patched is None
    assert "写操作" in detail


def test_drop_step_allows_plain_step():
    skill = make_skill()
    ctx = HealContext(failed_step=type("S", (), {"step_id": 1})())
    patched, detail = _op_drop_optional_step(skill, ctx)
    assert patched is not None, detail
    assert [s.step_id for s in patched.steps] == [1]
    assert patched.steps[0].action is ActionKind.CLICK


# --------------------------------------------------------------------------- #
# 定位器候选
# --------------------------------------------------------------------------- #
def test_collect_locator_candidates_ranks_by_fingerprint():
    page = FakePage(
        fingerprints=[
            ElementFingerprint(role="textbox", accessible_name="金额", neighbor_text="发票"),
            SUBMIT_FP,
        ]
    )
    skill = make_skill()
    found = asyncio.run(collect_locator_candidates(page, skill))
    assert 2 in found
    assert found[2][0].name == "提交", f"最像的应该排第一：{found[2]}"


def test_collect_locator_candidates_skips_low_similarity():
    page = FakePage(
        fingerprints=[ElementFingerprint(role="textbox", accessible_name="完全无关")]
    )
    skill = make_skill()
    found = asyncio.run(collect_locator_candidates(page, skill))
    assert 2 not in found


# --------------------------------------------------------------------------- #
# Golden 用例生成
# --------------------------------------------------------------------------- #
def test_suggest_cases_reaches_limit():
    skill = make_skill()
    skill.parameters = [
        Parameter(name="客户名称", type="string", example="张三"),
        Parameter(name="金额", type="int", example=500),
        Parameter(name="日期", type="date", example="2024-01-15"),
    ]
    cases = suggest_cases(skill, limit=12)
    # 每个参数的变体数是有限的，凑不满就按实际给（limit 是上限不是配额）
    assert len(cases) >= 10, f"只生成了 {len(cases)} 条，太少没意义"
    assert len(suggest_cases(skill, limit=5)) == 5
    assert cases[0].case_id.endswith("base")
    ids = [c.case_id for c in cases]
    assert len(set(ids)) == len(ids), "case_id 必须唯一"
    assert all(c.origin == "manual_edge" for c in cases)
    # 参数扰动真的发生了
    assert any(c.params.get("金额") != 500 for c in cases)


def test_suggest_cases_respects_base_params():
    skill = make_skill()
    cases = suggest_cases(skill, base_params={"客户名称": "李四"}, limit=3)
    assert cases[0].params["客户名称"] == "李四"


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

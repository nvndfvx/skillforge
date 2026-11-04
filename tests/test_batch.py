"""离线测试：批量执行（失败隔离 / 断点续跑 / 节流 / 报告）。

    python tests/test_batch.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from skillforge.batch import BatchItem, load_items, run_batch  # noqa: E402
from skillforge.schema.models import Assertion, Parameter, Precondition, Skill, Step  # noqa: E402
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    Locator,
    WriteEffect,
)
from skillforge.testing import DEFAULT_RECEIPT, FakePage  # noqa: E402


def make_skill() -> Skill:
    return Skill(
        skill_id="expense",
        name="报销单录入",
        parameters=[Parameter(name="客户名称", type="string")],
        preconditions=[Precondition(kind="on_page", locator_or_path="http://erp.local/expense/new")],
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


def factory_failing_on(bad_ids):
    """bad_ids 里的条目提交后拿不到单号 → 业务断言失败。"""

    def factory(item):
        bad = item.item_id in bad_ids
        return FakePage(
            on_submit_text="提交失败：服务器繁忙" if bad else DEFAULT_RECEIPT,
            required_fields=("客户名称",),
        )

    return factory


def items(*ids):
    return [BatchItem(item_id=i, params={"客户名称": f"客户{i}"}) for i in ids]


def test_all_good():
    report = asyncio.run(run_batch(make_skill(), items("a", "b", "c"), factory_failing_on(set())))
    assert report.total == 3
    assert report.passed == 3, report.to_dict()
    assert report.failed == 0
    assert report.rate == 1.0
    assert all(r.duration_ms >= 0 for r in report.results)


def test_one_failure_does_not_stop_the_batch():
    """批量执行和跑测试最本质的区别：一条失败绝不能中断整批。"""
    report = asyncio.run(
        run_batch(make_skill(), items("a", "bad", "c"), factory_failing_on({"bad"}))
    )
    assert report.total == 3, "三条都要跑"
    assert report.passed == 2
    assert report.failed == 1
    assert report.rate == round(2 / 3, 4)
    bad = [r for r in report.results if r.item_id == "bad"][0]
    assert not bad.ok
    assert bad.failure_layer, "失败要能定位到层"
    assert bad.step_id == 2


def test_checkpoint_resume_skips_success_and_keeps_rate_honest():
    report = asyncio.run(
        run_batch(
            make_skill(),
            items("a", "b", "c"),
            factory_failing_on(set()),
            checkpoint={"a": "ok", "b": "failed"},
        )
    )
    assert report.skipped == 1, "只有上次成功的那条才跳过"
    assert report.attempted == 2
    assert report.passed == 2
    assert report.rate == 1.0, "跳过的不能进分母，否则续跑会虚高通过率"
    assert "a" not in [r.item_id for r in report.results if r.status != "skipped"]


def test_limit_counts_attempted_not_skipped():
    report = asyncio.run(
        run_batch(
            make_skill(),
            items("a", "b", "c"),
            factory_failing_on(set()),
            checkpoint={"a": "ok"},
            limit=2,
        )
    )
    attempted = [r for r in report.results if r.status != "skipped"]
    assert len(attempted) == 2, [r.item_id for r in report.results]


def test_stop_after_failures():
    report = asyncio.run(
        run_batch(
            make_skill(),
            items("bad1", "bad2", "bad3"),
            factory_failing_on({"bad1", "bad2", "bad3"}),
            stop_after_failures=1,
        )
    )
    assert report.total == 1, "失败到 1 条就该停"
    assert report.failed == 1


def test_factory_exception_is_isolated():
    """打开页面就炸（比如浏览器崩了），也只算这一条失败。"""

    def factory(item):
        if item.item_id == "boom":
            raise RuntimeError("browser crashed")
        return FakePage(required_fields=("客户名称",))

    report = asyncio.run(run_batch(make_skill(), items("a", "boom", "c"), factory))
    assert report.total == 3
    assert report.passed == 2
    boom = [r for r in report.results if r.item_id == "boom"][0]
    assert boom.status == "failed"
    assert boom.failure_layer == "executor"
    assert "browser crashed" in boom.detail


def test_teardown_called_per_item():
    closed = []
    pages = factory_failing_on(set())

    def factory(item):
        page = pages(item)
        page.item_id = item.item_id  # type: ignore[attr-defined]
        return page

    def teardown(page):
        closed.append(page.item_id)  # type: ignore[attr-defined]

    asyncio.run(run_batch(make_skill(), items("a", "b"), factory, teardown=teardown))
    assert closed == ["a", "b"]


def test_by_layer_and_report_shape():
    report = asyncio.run(
        run_batch(make_skill(), items("a", "bad"), factory_failing_on({"bad"}))
    )
    payload = report.to_dict()
    assert set(payload) == {
        "total", "attempted", "passed", "failed", "skipped", "rate", "by_layer", "results",
    }
    assert sum(payload["by_layer"].values()) == payload["failed"]
    assert {"item_id", "ok", "status", "detail", "duration_ms", "failure_layer", "step_id"} <= set(
        payload["results"][0]
    )
    assert report.checkpoint() == {"a": "ok"}, "续跑只记成功的"


def test_load_items_validation():
    parsed = load_items([{"item_id": "a", "params": {"x": 1}}, {"case_id": "b"}])
    assert [i.item_id for i in parsed] == ["a", "b"]
    assert parsed[0].params == {"x": 1}

    assert len(load_items({"items": [{"item_id": "a"}]})) == 1

    for bad, why in [
        ([], "空清单"),
        ([{"params": {}}], "缺 item_id"),
        ([{"item_id": "a"}, {"item_id": "a"}], "重复 item_id"),
        ("nope", "类型不对"),
    ]:
        try:
            load_items(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应该拒绝：{why}")


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

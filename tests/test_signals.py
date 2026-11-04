"""离线测试：H14 technical 探针的连续失败累积告警。

    python tests/test_signals.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import temp_dir  # noqa: E402

from skillforge.executor.driver import execute  # noqa: E402
from skillforge.schema.models import Assertion, Parameter, Skill, Step  # noqa: E402
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    Locator,
    WriteEffect,
)
from skillforge.signals import SignalBook  # noqa: E402
from skillforge.testing import FakePage  # noqa: E402

GOOD_URL = "http://erp.local/expense/success"
BAD_URL = "http://erp.local/expense/list"


def make_skill() -> Skill:
    return Skill(
        skill_id="expense",
        name="报销单录入",
        parameters=[Parameter(name="客户名称", type="string")],
        steps=[
            Step(
                step_id=1,
                action=ActionKind.CLICK,
                target=Locator(role="button", name="提交"),
                write_effect=WriteEffect.CREATE,
                idempotency_key="{{flow_run_id}}::1",
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
                    expected=r"R\-\d{6,}",
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
            )
        ],
        meta={"steps_reviewed": True},
        state="reviewed",
    )


def run_once(skill: Skill, *, good: bool):
    page = FakePage(on_submit_url=GOOD_URL if good else BAD_URL)
    return asyncio.run(execute(skill, {"客户名称": "张三"}, page))


def test_technical_failure_does_not_kill_but_is_counted():
    skill = make_skill()
    run = run_once(skill, good=False)
    assert run.ok, "technical 失败不该判死"
    assert run.steps[-1].technical_failures

    with temp_dir() as tmp:
        book = SignalBook(tmp, threshold=5)
        newly = book.record_run(skill, run)
        assert newly == [], "第 1 次不该升格"
        records = book.all_records("expense")
        assert len(records) == 1
        assert records[0].assertion_id == "t-url"
        assert records[0].consecutive == 1
        assert not records[0].escalated


def test_escalates_at_threshold_exactly_once():
    skill = make_skill()
    with temp_dir() as tmp:
        book = SignalBook(tmp, threshold=3)
        escalations = []
        for _ in range(5):
            escalations.extend(book.record_run(skill, run_once(skill, good=False)))
        assert len(escalations) == 1, f"只该升格一次，实际 {len(escalations)}"
        assert escalations[0].assertion_id == "t-url"
        assert escalations[0].consecutive == 3

        records = book.all_records("expense")
        assert records[0].consecutive == 5
        assert records[0].total == 5
        assert records[0].escalated


def test_success_resets_counter_and_allows_reescalation():
    skill = make_skill()
    with temp_dir() as tmp:
        book = SignalBook(tmp, threshold=2)
        book.record_run(skill, run_once(skill, good=False))
        first = book.record_run(skill, run_once(skill, good=False))
        assert len(first) == 1, "第 2 次该升格"

        # 环境恢复了
        book.record_run(skill, run_once(skill, good=True))
        rec = book.all_records("expense")[0]
        assert rec.consecutive == 0
        assert not rec.escalated, "恢复了就该清掉升格标记"

        # 再次恶化，应该能重新升格
        book.record_run(skill, run_once(skill, good=False))
        again = book.record_run(skill, run_once(skill, good=False))
        assert len(again) == 1, "复发要能重新升格"


def test_persists_across_instances():
    skill = make_skill()
    with temp_dir() as tmp:
        SignalBook(tmp, threshold=5).record_run(skill, run_once(skill, good=False))
        book2 = SignalBook(tmp, threshold=5)
        records = book2.all_records("expense")
        assert len(records) == 1 and records[0].consecutive == 1


def test_report_buckets():
    skill = make_skill()
    with temp_dir() as tmp:
        book = SignalBook(tmp, threshold=2)
        book.record_run(skill, run_once(skill, good=False))
        report = book.report("expense")
        assert report["threshold"] == 2
        assert len(report["watching"]) == 1
        assert report["escalated"] == []

        book.record_run(skill, run_once(skill, good=False))
        report = book.report("expense")
        assert len(report["escalated"]) == 1
        assert report["watching"] == []


def test_reset():
    skill = make_skill()
    with temp_dir() as tmp:
        book = SignalBook(tmp, threshold=1)
        book.record_run(skill, run_once(skill, good=False))
        assert book.all_records("expense")
        assert book.reset("expense") == 1
        assert book.all_records("expense") == []
        assert book.reset("expense") == 0


def test_cli_signals_reports_and_gates():
    from skillforge.cli import main
    import contextlib
    import io
    import json
    from skillforge.registry import SkillStore

    skill = make_skill()
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        book = SignalBook(store.root, threshold=2)
        book.record_run(skill, run_once(skill, good=False))
        book.record_run(skill, run_once(skill, good=False))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["signals", "--store", str(store.root), "--skill-id", "expense",
                         "--fail-on-escalated"])
        out = buf.getvalue()
        assert code == 1, out
        assert "t-url" in out
        payload = json.loads(out.split("\n⚠")[0])
        assert len(payload["escalated"]) == 1

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["signals", "--store", str(store.root), "--skill-id", "expense",
                         "--reset"])
        assert code == 0
        assert "已清零" in buf.getvalue()
        assert book.all_records("expense") == []


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

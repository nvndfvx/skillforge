"""离线测试：版本库（发布闸 / 回滚 / Golden Set 只增不删）+ JSON 往返。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import temp_dir  # noqa: E402

from skillforge.compiler.distiller import distill  # noqa: E402
from skillforge.observer.aligner import align_actions  # noqa: E402
from skillforge.registry import (  # noqa: E402
    GateRejected,
    SkillStore,
    bump_minor,
    load_skill,
    save_skill,
)
from skillforge.schema.models import CaseScore, GoldenCase  # noqa: E402
from skillforge.schema.serde import golden_case_from_dict, skill_from_dict  # noqa: E402
from skillforge.schema.types import RawAction  # noqa: E402


def act(idx, action, name="", value=None, role="", url="", before="", after=""):
    return RawAction(
        seq=idx, action=action, name=name, value=value, role=role, url=url,
        before_text=before, after_text=after,
    )


BASE = "http://erp.local/expense/new"


def demo(customer, amount):
    return [
        act(0, "goto", role="document", url=BASE),
        act(1, "fill", "客户名称", customer, role="textbox", url=BASE),
        act(2, "fill", "金额", amount, role="textbox", url=BASE),
        act(3, "click", "提交", role="button", url=BASE + "/ok",
            before="填写中", after="提交成功 单号 R-20240115 已创建"),
    ]


def make_skill():
    a, b = demo("张三", "500"), demo("李四", "800")
    return distill(align_actions(a, b), a, skill_id="expense", name="报销单录入").skill


def scores(*pairs):
    return [CaseScore(case_id=c, passed=p) for c, p in pairs]


def test_bump_minor():
    assert bump_minor("1.0.0") == "1.1.0"
    assert bump_minor("1.9.3") == "1.10.0"
    assert bump_minor("2.0") == "2.1.0"


def test_serde_round_trip():
    skill = make_skill()
    again = skill_from_dict(skill.to_dict())
    assert again.to_dict() == skill.to_dict()

    case = GoldenCase(case_id="c1", params={"客户名称": "张三"})
    assert golden_case_from_dict(case.to_dict()).to_dict() == case.to_dict()


def test_save_and_load_skill_file():
    with temp_dir() as tmp:
        path = Path(tmp) / "skill.json"
        skill = make_skill()
        save_skill(skill, path)
        loaded = load_skill(path)
        assert loaded.to_dict() == skill.to_dict()


def test_first_release_requires_all_pass():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        skill = make_skill()
        try:
            store.release(skill, scores(("c1", True), ("c2", False)))
        except GateRejected as exc:
            assert any("首版" in r for r in exc.gate.reasons)
        else:
            raise AssertionError("首版没有全过，不该发布")
        assert store.current_record("expense") is None


def test_first_release_ok():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        skill = make_skill()
        record, gate = store.release(skill, scores(("c1", True), ("c2", True)))
        assert record.version == "1.0.0"
        assert record.rate == 1.0
        assert gate.passed
        assert store.current_record("expense").version == "1.0.0"
        assert store.load_skill("expense") is not None


def test_release_rejects_regression_and_writes_nothing():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        skill = make_skill()
        store.release(skill, scores(("c1", True), ("c2", True)))

        before = (Path(tmp) / "expense" / "store.json").read_text(encoding="utf-8")
        try:
            store.release(skill, scores(("c1", True), ("c2", False)))
        except GateRejected as exc:
            assert any("单点翻转" in r for r in exc.gate.reasons)
        else:
            raise AssertionError("退化了还发布")

        after = (Path(tmp) / "expense" / "store.json").read_text(encoding="utf-8")
        assert before == after, "被闸拒绝时库里不能有任何改动"
        assert store.current_record("expense").version == "1.0.0"


def test_release_accepts_improvement_and_bumps_version():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        skill = make_skill()
        store.release(skill, scores(("c1", True)))
        record, _gate = store.release(skill, scores(("c1", True), ("c2", True)))
        assert record.version == "1.1.0", record.version
        assert store.current_record("expense").version == "1.1.0"


def test_rollback_moves_pointer_and_keeps_history():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        skill = make_skill()
        store.release(skill, scores(("c1", True)))
        store.release(skill, scores(("c1", True), ("c2", True)))
        assert store.current_record("expense").version == "1.1.0"

        record = store.rollback("expense")
        assert record.version == "1.0.0"
        assert store.current_record("expense").version == "1.0.0"
        # 历史不删，还能再切回去
        assert [r.version for r in store.history("expense")] == ["1.0.0", "1.1.0"]
        store.rollback("expense", to="1.1.0")
        assert store.current_record("expense").version == "1.1.0"


def test_rollback_at_earliest_fails():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        store.release(make_skill(), scores(("c1", True)))
        try:
            store.rollback("expense")
        except ValueError as exc:
            assert "最早" in str(exc)
        else:
            raise AssertionError("只有一版还能回滚？")


def test_golden_set_is_append_only():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        store.add_case("expense", GoldenCase(case_id="c1", params={"客户名称": "张三"}))
        store.add_case("expense", GoldenCase(case_id="c2", params={"客户名称": "李四"}))
        assert [c.case_id for c in store.cases("expense")] == ["c1", "c2"]
        try:
            store.add_case("expense", GoldenCase(case_id="c1", params={}))
        except ValueError as exc:
            assert "重复" in str(exc)
        else:
            raise AssertionError("只增不删：重复 case_id 必须拒绝")


def test_status():
    with temp_dir() as tmp:
        store = SkillStore(tmp)
        store.add_case("expense", GoldenCase(case_id="c1"))
        store.release(make_skill(), scores(("c1", True)))
        status = store.status("expense")
        assert status["current"] == "1.0.0"
        assert status["golden_cases"] == 1
        assert status["releases"] == ["1.0.0"]


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

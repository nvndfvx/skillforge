"""离线测试：灰度发布（粘性路由 / 按版本统计 / 结论判定）。

    python tests/test_canary.py
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

from _helpers import temp_dir  # noqa: E402

from skillforge.batch import run_batch  # noqa: E402
from skillforge.canary import CanaryBook, CanaryState, bucket  # noqa: E402
from test_batch import factory_failing_on, items, make_skill  # noqa: E402


def test_bucket_is_stable_and_deterministic():
    """必须是稳定哈希：Python 内置 hash() 带随机种子，跨进程就不一致。"""
    keys = [f"item-{i}" for i in range(200)]
    first = [bucket("expense", k) for k in keys]
    second = [bucket("expense", k) for k in keys]
    assert first == second, "同一进程内两次必须一致"
    assert all(0 <= b < 10_000 for b in first)
    assert len(set(first)) > 150, "分桶要够散"
    # 换 skill_id 应该换一套分桶
    assert [bucket("other", k) for k in keys] != first


def test_routing_is_sticky():
    state = CanaryState(skill_id="expense", candidate_version="1.1.0", weight=0.3)
    answers = {state.routes("s1") for _ in range(20)}
    assert len(answers) == 1, "同一个 key 必须永远给同一个答案，否则同一张单会被提交两次"


def test_actual_weight_near_target():
    state = CanaryState(skill_id="expense", candidate_version="1.1.0", weight=0.1)
    keys = [f"s{i}" for i in range(2000)]
    ratio = state.actual_weight(keys)
    assert 0.06 <= ratio <= 0.14, f"2000 个 key 下 10% 分流偏得太多：{ratio}"

    zero = CanaryState(skill_id="expense", candidate_version="1.1.0", weight=0.0)
    assert zero.actual_weight(keys) == 0.0
    full = CanaryState(skill_id="expense", candidate_version="1.1.0", weight=1.0)
    assert full.actual_weight(keys) == 1.0


def test_verdict_wait_then_promote():
    state = CanaryState(skill_id="expense", candidate_version="1.1.0", min_samples=5)
    for _ in range(10):
        state.record("1.0.0", True)

    state.record("1.1.0", True)
    verdict = state.verdict("1.0.0")
    assert verdict["decision"] == "wait", verdict
    assert verdict["enough_samples"] is False
    assert verdict["stable"] == {"version": "1.0.0", "samples": 10, "rate": 1.0}

    for _ in range(4):
        state.record("1.1.0", True)
    verdict = state.verdict("1.0.0")
    assert verdict["decision"] == "promote", verdict
    assert verdict["candidate"]["samples"] == 5


def test_verdict_abort_when_candidate_worse():
    state = CanaryState(skill_id="expense", candidate_version="1.1.0", min_samples=5)
    for _ in range(10):
        state.record("1.0.0", True)
    for _ in range(3):
        state.record("1.1.0", True)
    for _ in range(3):
        state.record("1.1.0", False)
    verdict = state.verdict("1.0.0")
    assert verdict["decision"] == "abort", verdict
    assert "低于稳定版" in verdict["reason"]


def test_statistics_do_not_leak_between_versions():
    """候选的失败绝不能算到稳定版头上——否则灰度结论就是错的。"""
    state = CanaryState(skill_id="expense", candidate_version="1.1.0", min_samples=2)
    state.record("1.0.0", True)
    state.record("1.1.0", False)
    assert state.samples["1.0.0"] == {"ok": 1, "fail": 0}
    assert state.samples["1.1.0"] == {"ok": 0, "fail": 1}


def test_book_roundtrip_and_clear():
    state = CanaryState(
        skill_id="expense", candidate_version="1.1.0", weight=0.1, min_samples=7
    )
    state.record("1.0.0", True)
    with temp_dir() as tmp:
        book = CanaryBook(tmp)
        assert book.load("expense") is None
        book.save(state)
        loaded = book.load("expense")
        assert loaded is not None
        assert loaded.candidate_version == "1.1.0"
        assert loaded.min_samples == 7
        assert loaded.samples["1.0.0"] == {"ok": 1, "fail": 0}

        book.candidate_path("expense").parent.mkdir(parents=True, exist_ok=True)
        book.candidate_path("expense").write_text("{}", encoding="utf-8")
        assert book.clear("expense") is True
        assert book.load("expense") is None
        assert not book.candidate_path("expense").exists()
        assert book.clear("expense") is False


def test_batch_resolver_routes_and_counts_per_version():
    stable = make_skill()
    stable.version = "1.0.0"
    candidate = make_skill()
    candidate.version = "1.1.0"

    def resolver(item):
        return candidate if item.item_id.endswith("2") else stable

    report = asyncio.run(
        run_batch(
            stable,
            items("s1", "s2", "s3"),
            factory_failing_on(set()),
            resolver=resolver,
        )
    )
    assert [r.version for r in report.results] == ["1.0.0", "1.1.0", "1.0.0"]
    assert report.by_version() == {
        "1.0.0": {"ok": 2, "fail": 0},
        "1.1.0": {"ok": 1, "fail": 0},
    }


def test_by_version_ignores_skipped():
    stable = make_skill()
    stable.version = "1.0.0"
    report = asyncio.run(
        run_batch(
            stable,
            items("s1", "s2"),
            factory_failing_on(set()),
            checkpoint={"s1": "ok"},
        )
    )
    assert report.by_version() == {"1.0.0": {"ok": 1, "fail": 0}}, "跳过的不能进灰度样本"


def test_param_mode_is_sticky_per_user():
    """按参数分流：同一个用户的多张单必须落在同一个版本。"""
    state = CanaryState(
        skill_id="expense", candidate_version="1.1.0", weight=0.5,
        mode="param", field="客户编号",
    )
    for uid in ("U1", "U2", "U3", "U4", "U5"):
        answers = {state.routes(f"item-{i}", {"客户编号": uid}) for i in range(10)}
        assert len(answers) == 1, f"用户 {uid} 的单被分到了两个版本"

    decisions = {state.routes("x", {"客户编号": f"U{i}"}) for i in range(40)}
    assert decisions == {True, False}, "不同用户之间应该分化"


def test_segment_mode_only_whitelisted():
    """分群模式：只放行白名单，100%，不看 weight。"""
    state = CanaryState(
        skill_id="expense", candidate_version="1.1.0",
        mode="segment", field="租户", segments=["A公司", "B公司"],
    )
    assert state.routes("i1", {"租户": "A公司"})
    assert state.routes("i1", {"租户": "B公司"})
    assert not state.routes("i1", {"租户": "C公司"})
    assert state.actual_weight(["A公司", "C公司"]) == 0.5
    assert "只放行" in state.describe()


def test_missing_param_falls_back_to_item_id():
    """缺参数不能崩，也不能悄悄退化成全 0% / 全 100%。"""
    state = CanaryState(
        skill_id="expense", candidate_version="1.1.0", weight=0.5,
        mode="param", field="客户编号",
    )
    assert state.route_key("item-7", {}) == "item-7"
    assert state.route_key("item-7", {"客户编号": None}) == "item-7"
    assert state.route_key("item-7", {"客户编号": ""}) == "item-7"
    assert state.route_key("item-7", {"客户编号": "U9"}) == "U9"
    # 退回 item_id 之后仍然确定
    assert state.routes("item-7", {}) == state.routes("item-7", {})


def test_modes_survive_roundtrip():
    state = CanaryState(
        skill_id="expense", candidate_version="1.1.0",
        mode="segment", field="租户", segments=["A", "B"],
    )
    again = CanaryState.from_dict(state.to_dict())
    assert again.mode == "segment"
    assert again.field == "租户"
    assert again.segments == ["A", "B"]
    assert again.routes("i", {"租户": "A"})


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

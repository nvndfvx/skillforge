"""离线测试：跨页关联断言的自动推断。

    python tests/test_entity_binding.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from skillforge.compiler.distiller import id_token, infer_entity_checks  # noqa: E402
from skillforge.schema.models import Step  # noqa: E402
from skillforge.schema.types import ActionKind, Locator  # noqa: E402


def _click(step_id: int, name: str, tab: int = 0) -> Step:
    return Step(step_id=step_id, action=ActionKind.CLICK,
                target=Locator(role="link", name=name), tab=tab)


def test_id_token_shapes():
    assert id_token("任务 R-20240115 待审核") == "R-20240115"
    assert id_token("单据号 12345678") == "12345678"
    assert id_token("TASK_998877") == "TASK_998877"
    assert id_token("审核意见") == ""


def test_infers_when_param_shares_shape():
    """点开的行里有任务号，且确实有个参数带着同样形态的号 → 自动挂关联断言。"""
    steps = [
        _click(1, "任务 R-20240115 待审核", tab=0),
        Step(step_id=2, action=ActionKind.FILL,
             target=Locator(role="textbox", name="审核意见"), tab=1),
    ]
    warnings: list[str] = []
    added = infer_entity_checks(steps, {"任务号": "R-20240115", "金额": "500"}, warnings)
    assert added == 1, added
    check = steps[1].entity_check
    assert check is not None
    assert check.param == "任务号"
    # 形态要泛化：不能把 R-20240115 写死成字面量
    assert "20240115" not in check.pattern
    assert warnings == []


def test_never_invents_when_no_param_matches():
    """没有参数带这个实体号时，**不许编一个假判据** —— 只给警告要人审补。"""
    steps = [
        _click(1, "任务 R-20240115 待审核", tab=0),
        _click(2, "通过", tab=1),
    ]
    warnings: list[str] = []
    added = infer_entity_checks(steps, {"金额": "500"}, warnings)
    assert added == 0
    assert steps[1].entity_check is None
    assert len(warnings) == 1
    assert "人审要补" in warnings[0]


def test_ambiguous_match_is_not_guessed():
    """两个参数都像时也不猜 —— 猜错等于给错误判据背书。"""
    steps = [
        _click(1, "任务 R-20240115 待审核", tab=0),
        Step(step_id=2, action=ActionKind.FILL,
             target=Locator(role="textbox", name="审核意见"), tab=1),
    ]
    warnings: list[str] = []
    added = infer_entity_checks(steps, {"任务号": "R-20240115", "备份号": "R-20240116"}, warnings)
    assert added == 0
    assert steps[1].entity_check is None
    assert "多个参数都像" in warnings[0]


def test_single_tab_steps_untouched():
    """同页流程不需要关联断言，别乱加。"""
    steps = [_click(1, "提交", tab=0)]
    warnings: list[str] = []
    assert infer_entity_checks(steps, {"任务号": "R-20240115"}, warnings) == 0
    assert steps[0].entity_check is None
    assert warnings == []


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

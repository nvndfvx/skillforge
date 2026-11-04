"""离线测试：CLI 的完整流程（compile → review → golden → release → rollback）。

    python tests/test_cli.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import temp_dir  # noqa: E402

from skillforge.cli import main  # noqa: E402
from skillforge.registry import load_skill, save_skill  # noqa: E402
from skillforge.schema.types import RawAction, RawTrace  # noqa: E402

BASE = "http://erp.local/expense/new"


def _act(idx, action, name="", value=None, role="", url="", before="", after=""):
    return RawAction(
        seq=idx, action=action, name=name, value=value, role=role, url=url,
        before_text=before, after_text=after,
    )


def _trace(trace_id: str, customer: str, amount: str) -> RawTrace:
    return RawTrace(
        trace_id=trace_id,
        started_at="2024-01-01T00:00:00+00:00",
        url=BASE,
        actions=[
            _act(0, "goto", role="document", url=BASE),
            _act(1, "fill", "客户名称", customer, role="textbox", url=BASE),
            _act(2, "fill", "金额", amount, role="textbox", url=BASE),
            _act(3, "click", "提交", role="button", url=BASE + "/ok",
                 before="填写中", after="提交成功 单号 R-20240115 已创建"),
        ],
    )


def run_cli(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(list(argv))
    return code, buf.getvalue()


def _setup_traces(tmp: Path) -> tuple[Path, Path]:
    a = tmp / "a.json"
    b = tmp / "b.json"
    a.write_text(json.dumps(_trace("a", "张三", "500").to_dict(), ensure_ascii=False), encoding="utf-8")
    b.write_text(json.dumps(_trace("b", "李四", "800").to_dict(), ensure_ascii=False), encoding="utf-8")
    return a, b


def test_full_cli_flow():
    with temp_dir() as tmpdir:
        tmp = Path(tmpdir)
        store = tmp / "store"
        skill_path = tmp / "skill.json"
        a, b = _setup_traces(tmp)

        # 1. compile
        code, out = run_cli(
            "compile", "--trace-a", str(a), "--trace-b", str(b),
            "--out", str(skill_path), "--skill-id", "expense", "--name", "报销单录入",
        )
        assert code == 0, out
        assert skill_path.exists()
        assert "客户名称" in out

        # 2. review：只打两道勾 → 还有阻断
        code, out = run_cli(
            "review", "--skill", str(skill_path),
            "--steps-reviewed", "--confirm-assertions",
        )
        assert code == 1, out
        assert "precheck" in out

        # 3. review：补第三道勾 → 可以发布
        code, out = run_cli(
            "review", "--skill", str(skill_path),
            "--steps-reviewed", "--confirm-assertions", "--precheck", r"R\-\d{6,}",
        )
        assert code == 0, out
        assert "可以 release 了" in out

        # 4. golden add / list
        code, out = run_cli(
            "golden", "add", "--store", str(store), "--skill-id", "expense",
            "--case-id", "c1", "--params", '{"客户名称":"张三","金额":"500"}',
        )
        assert code == 0, out
        code, out = run_cli(
            "golden", "list", "--store", str(store), "--skill-id", "expense"
        )
        assert code == 0, out
        assert "c1" in out

        # 5. release 首版：有失败 → 拒绝
        bad = tmp / "bad.json"
        bad.write_text(json.dumps([{"case_id": "c1", "passed": False}]), encoding="utf-8")
        code, out = run_cli(
            "release", "--store", str(store), "--skill", str(skill_path), "--scores", str(bad)
        )
        assert code == 1, out
        assert "单调性闸" in out

        # 6. release 首版：全过 → v1.0.0
        good = tmp / "good.json"
        good.write_text(json.dumps({"cases": [{"case_id": "c1", "passed": True}]}), encoding="utf-8")
        code, out = run_cli(
            "release", "--store", str(store), "--skill", str(skill_path), "--scores", str(good)
        )
        assert code == 0, out
        assert "v1.0.0" in out

        # 7. status
        code, out = run_cli("status", "--store", str(store), "--skill-id", "expense")
        assert code == 0
        assert "1.0.0" in out

        # 8. release 第二版：多一条用例全过 → v1.1.0
        two = tmp / "two.json"
        two.write_text(
            json.dumps([{"case_id": "c1", "passed": True}, {"case_id": "c2", "passed": True}]),
            encoding="utf-8",
        )
        code, out = run_cli(
            "release", "--store", str(store), "--skill", str(skill_path), "--scores", str(two)
        )
        assert code == 0, out
        assert "v1.1.0" in out

        # 9. 退化 → 拒绝，且库里不变
        store_before = (store / "expense" / "store.json").read_text(encoding="utf-8")
        regress = tmp / "regress.json"
        regress.write_text(
            json.dumps([{"case_id": "c1", "passed": True}, {"case_id": "c2", "passed": False}]),
            encoding="utf-8",
        )
        code, out = run_cli(
            "release", "--store", str(store), "--skill", str(skill_path), "--scores", str(regress)
        )
        assert code == 1, out
        assert (store / "expense" / "store.json").read_text(encoding="utf-8") == store_before

        # 10. rollback
        code, out = run_cli("rollback", "--store", str(store), "--skill-id", "expense")
        assert code == 0, out
        assert "v1.0.0" in out


def test_release_refuses_unreviewed_skill():
    with temp_dir() as tmpdir:
        tmp = Path(tmpdir)
        a, b = _setup_traces(tmp)
        skill_path = tmp / "skill.json"
        run_cli(
            "compile", "--trace-a", str(a), "--trace-b", str(b),
            "--out", str(skill_path), "--skill-id", "expense",
        )
        good = tmp / "good.json"
        good.write_text(json.dumps([{"case_id": "c1", "passed": True}]), encoding="utf-8")
        code, out = run_cli(
            "release", "--store", str(tmp / "store"), "--skill", str(skill_path),
            "--scores", str(good),
        )
        assert code == 1, out
        assert "人审" in out


def _params_skill():
    from skillforge.schema.models import Parameter, Skill, Step
    from skillforge.schema.types import ActionKind, Locator

    return Skill(
        skill_id="expense",
        name="报销单录入",
        parameters=[
            Parameter(name="客户名称", type="string", example="张三"),
            Parameter(name="金额", type="int", example=500),
        ],
        steps=[
            Step(step_id=1, action=ActionKind.FILL,
                 target=Locator(role="textbox", name="客户名称"), value="{{客户名称}}")
        ],
    )


def test_compiled_skill_starts_as_draft():
    with temp_dir() as tmp:
        a, b = _setup_traces(tmp)
        skill_path = tmp / "skill.json"
        run_cli(
            "compile", "--trace-a", str(a), "--trace-b", str(b),
            "--out", str(skill_path), "--skill-id", "expense",
        )
        assert load_skill(skill_path).state == "draft"


def test_golden_suggest_then_apply():
    with temp_dir() as tmp:
        skill_path = tmp / "skill.json"
        save_skill(_params_skill(), skill_path)
        store = tmp / "store"

        code, out = run_cli(
            "golden", "suggest", "--store", str(store), "--skill-id", "expense",
            "--skill", str(skill_path), "--limit", "6",
        )
        assert code == 0, out
        assert "必须人确认" in out
        assert "没有写库" in out

        code, out = run_cli(
            "golden", "suggest", "--store", str(store), "--skill-id", "expense",
            "--skill", str(skill_path), "--limit", "6", "--apply",
        )
        assert code == 0, out
        assert "已写入" in out

        code, out = run_cli("golden", "list", "--store", str(store), "--skill-id", "expense")
        assert code == 0, out
        assert "origin=manual_edge" in out


def test_heal_refuses_without_golden_set():
    """heal 在空 Golden Set 上必须直接拒绝——没有判据就不该自动改。"""
    with temp_dir() as tmp:
        skill_path = tmp / "skill.json"
        save_skill(_params_skill(), skill_path)
        code, out = run_cli(
            "heal", "--store", str(tmp / "store"), "--skill", str(skill_path),
            "--start-url", "file:///tmp/none.html",
        )
        assert code == 1, out
        assert "Golden Set 是空的" in out


def test_review_interactive():
    """交互式人审：三道勾逐条问，答完就能发布。"""
    import builtins

    with temp_dir() as tmp:
        a, b = _setup_traces(tmp)
        skill_path = tmp / "skill.json"
        run_cli(
            "compile", "--trace-a", str(a), "--trace-b", str(b),
            "--out", str(skill_path), "--skill-id", "expense",
        )
        # 顺序：① 步骤 → ② 两条断言 → ③ precheck
        answers = iter(["y", "y", "y", r"R\-\d{6,}"])
        original = builtins.input
        builtins.input = lambda prompt="": next(answers)
        try:
            code, out = run_cli("review", "--skill", str(skill_path), "--interactive")
        finally:
            builtins.input = original

        assert code == 0, out
        assert "① 步骤清单" in out
        assert "可以 release 了" in out
        skill = load_skill(skill_path)
        assert skill.state == "reviewed"
        assert all(skill.review_checklist().values()), skill.review_checklist()
        assert skill.steps[-1].precheck is not None
        assert skill.steps[-1].precheck.expected == r"R\-\d{6,}"


def test_review_interactive_rejects_bad_steps():
    """第①道勾答「否」→ 状态留在 draft，不许发布。"""
    import builtins

    with temp_dir() as tmp:
        a, b = _setup_traces(tmp)
        skill_path = tmp / "skill.json"
        run_cli(
            "compile", "--trace-a", str(a), "--trace-b", str(b),
            "--out", str(skill_path), "--skill-id", "expense",
        )
        answers = iter(["n", "y", "y", r"R\-\d{6,}"])
        original = builtins.input
        builtins.input = lambda prompt="": next(answers)
        try:
            code, out = run_cli("review", "--skill", str(skill_path), "--interactive")
        finally:
            builtins.input = original

        assert code == 1, out
        assert "顺序或步骤有问题" in out
        skill = load_skill(skill_path)
        assert skill.state == "draft"
        assert skill.review_checklist()["① 步骤顺序已人审"] is False


def test_override_start_url():
    """命令行 --start-url 必须真的覆盖技能里的起始页，否则这个参数形同虚设。"""
    from skillforge.cli import _override_start_url
    from skillforge.schema.models import Precondition, Skill

    skill = Skill(
        skill_id="s",
        name="n",
        preconditions=[Precondition(kind="on_page", locator_or_path="file:///recorded.html")],
    )
    _override_start_url(skill, "file:///other.html")
    assert skill.preconditions[0].locator_or_path == "file:///other.html"

    # 空值不覆盖
    _override_start_url(skill, "")
    assert skill.preconditions[0].locator_or_path == "file:///other.html"
    # 非 on_page 的前置条件不动
    skill.preconditions.append(Precondition(kind="logged_in", locator_or_path="x"))
    _override_start_url(skill, "file:///third.html")
    assert skill.preconditions[1].locator_or_path == "x"


def main_() -> int:
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
    raise SystemExit(main_())

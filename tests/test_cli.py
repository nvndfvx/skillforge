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

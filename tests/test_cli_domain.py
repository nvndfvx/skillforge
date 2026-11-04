"""离线测试：CLI 域（跑真命令，不用浏览器）。

    python tests/test_cli_domain.py
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

from skillforge.cli_domain import OutputPage, run_cli_skill, run_command  # noqa: E402
from skillforge.schema.models import Assertion, Parameter, Skill, Step  # noqa: E402
from skillforge.schema.types import (  # noqa: E402
    ActionKind,
    AnchorKind,
    AssertionKind,
    Locator,
    WriteEffect,
)

PY = f'"{sys.executable}"'


def py_cmd(code: str) -> str:
    """跨平台的 python 命令（测试里只用单引号，避免 cmd.exe 的引号地狱）。"""
    return f"{PY} -c \"{code}\""


def assertion(expected: str = r"R\-\d{6,}", kind: AssertionKind = AssertionKind.BUSINESS) -> Assertion:
    return Assertion(
        assertion_id=f"a-{expected[:6]}",
        kind=kind,
        anchor=AnchorKind.BUSINESS_ENTITY if kind is AssertionKind.BUSINESS else AnchorKind.UI_TEXT,
        operator="entity_exists",
        expected=expected,
        confirmed_by="human",
    )


def make_skill(
    command: str,
    *,
    write: bool = False,
    precheck_command: str = "",
    expect: str = r"R\-\d{6,}",
    assertion_obj: Assertion | None = None,
) -> Skill:
    return Skill(
        skill_id="cli-demo",
        name="CLI 演示",
        domain="cli",
        parameters=[Parameter(name="单号", type="string")],
        steps=[
            Step(
                step_id=1,
                action=ActionKind.RUN,
                target=Locator(),
                value=command,
                write_effect=WriteEffect.CREATE if write else WriteEffect.NONE,
                idempotency_key="{{flow_run_id}}::1" if write else None,
                precheck_command=precheck_command,
                assertion=assertion_obj if assertion_obj is not None else assertion(expect),
            )
        ],
        meta={"steps_reviewed": True},
        state="reviewed",
    )


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def test_runs_command_and_asserts_output():
    skill = make_skill(py_cmd("print('创建成功 单号 R-20240115')"))
    result = asyncio.run(run_cli_skill(skill, {}))
    assert result.ok, result.to_dict()
    assert result.steps[0].status == "ok"
    assert "R-20240115" in result.steps[0].detail


def test_nonzero_exit_fails_at_step_layer():
    skill = make_skill(py_cmd("import sys; print('boom'); sys.exit(3)"))
    result = asyncio.run(run_cli_skill(skill, {}))
    assert not result.ok
    step = result.steps[0]
    assert step.failure_layer == "step"
    assert "返回 3" in step.detail and "boom" in step.detail


def test_business_assertion_failure_is_assertion_layer():
    """命令成功但没出现单号 → 判死（和浏览器域同一套分级）。"""
    skill = make_skill(py_cmd("print('一切正常')"))
    result = asyncio.run(run_cli_skill(skill, {}))
    assert not result.ok
    assert result.steps[0].failure_layer == "assertion"
    assert result.steps[0].business_failures


def test_technical_assertion_does_not_kill():
    technical = Assertion(
        assertion_id="t-out",
        kind=AssertionKind.TECHNICAL,
        anchor=AnchorKind.UI_TEXT,
        operator="contains",
        expected="不会出现的字符串",
        confirmed_by="human",
    )
    skill = make_skill(py_cmd("print('单号 R-20240115')"), assertion_obj=assertion())
    skill.steps[0].technical_assertion = technical
    result = asyncio.run(run_cli_skill(skill, {}))
    assert result.ok, "技术断言只报信，不判死"
    assert result.steps[0].technical_failures


# --------------------------------------------------------------------------- #
# 幂等门（CLI 域自己的写法）
# --------------------------------------------------------------------------- #
def test_write_without_precheck_command_is_blocked_and_does_not_run():
    with temp_dir() as tmp:
        marker = tmp / "created.txt"
        command = py_cmd(f"open(r'{marker}', 'w').write('x')")
        skill = make_skill(command, write=True, precheck_command="")
        result = asyncio.run(run_cli_skill(skill, {}, cwd=tmp))
        step = result.steps[0]
        assert step.status == "blocked", step
        assert step.failure_layer == "precheck"
        assert not marker.exists(), "被拦住就不许真的执行写操作"


def test_precheck_hit_skips_the_write():
    with temp_dir() as tmp:
        marker = tmp / "created.txt"
        command = py_cmd(f"open(r'{marker}', 'w').write('x')")
        skill = make_skill(
            command,
            write=True,
            precheck_command=py_cmd("print('已存在')"),  # 返回 0 = 已存在
        )
        result = asyncio.run(run_cli_skill(skill, {}, cwd=tmp))
        assert result.ok
        assert result.steps[0].status == "skipped"
        assert not marker.exists(), "查重命中就必须跳过，不能重复写"


def test_precheck_miss_runs_the_write():
    with temp_dir() as tmp:
        marker = tmp / "created.txt"
        command = py_cmd(f"open(r'{marker}', 'w').write('x'); print('单号 R-20240115')")
        skill = make_skill(
            command,
            write=True,
            precheck_command=py_cmd("import sys; sys.exit(1)"),  # 返回非 0 = 不存在
        )
        result = asyncio.run(run_cli_skill(skill, {}, cwd=tmp))
        assert result.ok, result.to_dict()
        assert result.steps[0].status == "ok"
        assert marker.exists(), "没查到就该真的写"


# --------------------------------------------------------------------------- #
# 参数 / 超时 / 输出适配
# --------------------------------------------------------------------------- #
def test_missing_param_fails_as_param_layer():
    skill = make_skill(py_cmd("print('单号 {{单号}}')"))
    result = asyncio.run(run_cli_skill(skill, {}))
    assert not result.ok
    assert result.steps[0].failure_layer == "param"
    assert "单号" in result.steps[0].detail


def test_param_is_rendered_into_command():
    skill = make_skill(py_cmd("print('单号 {{单号}}')"))
    result = asyncio.run(run_cli_skill(skill, {"单号": "R-20240116"}))
    assert result.ok, result.to_dict()
    assert "R-20240116" in result.steps[0].detail


def test_timeout_kills_and_fails():
    skill = make_skill(py_cmd("import time; time.sleep(5)"))
    result = asyncio.run(run_cli_skill(skill, {}, timeout_s=1))
    assert not result.ok
    assert "超时" in result.steps[0].detail


def test_output_page_only_supports_text_and_url():
    page = OutputPage("输出内容", "/tmp")

    async def probe():
        assert await page.text() == "输出内容"
        assert await page.url() == "/tmp"
        for method, args in (
            ("exists", (Locator(),)),
            ("count", (Locator(),)),
            ("goto", ("http://x",)),
            ("fill", (Locator(), "v")),
            ("click", (Locator(),)),
        ):
            try:
                await getattr(page, method)(*args)
            except NotImplementedError:
                pass
            else:
                raise AssertionError(f"{method} 应该明确不支持")

    asyncio.run(probe())


def test_run_command_captures_stderr_merged():
    outcome = asyncio.run(
        run_command(py_cmd("import sys; print('out'); print('err', file=sys.stderr)"))
    )
    assert outcome.ok
    assert "out" in outcome.output and "err" in outcome.output


# --------------------------------------------------------------------------- #
# 域与动作必须自洽
# --------------------------------------------------------------------------- #
def test_domain_and_action_must_match():
    cli_skill = make_skill(py_cmd("print('R-20240115')"))
    cli_skill.steps.append(
        Step(
            step_id=2,
            action=ActionKind.CLICK,
            target=Locator(role="button", name="提交"),
            assertion=assertion(),
        )
    )
    problems = cli_skill.errors()
    assert any("domain=cli 只能用 run" in p for p in problems), problems

    browser_skill = make_skill(py_cmd("print('R-20240115')"))
    browser_skill.domain = "browser"
    problems = browser_skill.errors()
    assert any("只在 domain=cli 下有效" in p for p in problems), problems

    bad_domain = make_skill(py_cmd("print('x')"))
    bad_domain.domain = "desktop-gui"
    assert any("未知执行域" in p for p in bad_domain.errors())


def test_run_step_requires_command_and_assertion():
    skill = make_skill("")
    problems = skill.errors()
    assert any("没有命令" in p for p in problems), problems

    no_assert = make_skill(py_cmd("print('x')"))
    no_assert.steps[0].assertion = None
    assert any("无断言" in p for p in no_assert.errors())


def _subprocess_available() -> bool:
    """DSH 沙箱禁止创建子进程（WinError 5），这种情况下整套测试跳过。

    CI 上正常跑；本地沙箱里跳过而不是假装通过，也不该让 run_all 变红。
    """
    try:
        outcome = asyncio.run(run_command(py_cmd("print(1)"), timeout_s=20))
    except (PermissionError, OSError):
        return False
    return outcome.ok


def main() -> int:
    if not _subprocess_available():
        print("SKIP 当前环境禁止创建子进程（DSH 沙箱）—— 请在普通终端或 CI 里跑本脚本")
        return 0

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

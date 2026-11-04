"""CLI 域：把技能跑在命令行上。

为什么值得做 —— 整条纵向调优链路（业务断言 / 幂等门 / 回归基准集 / 单调性闸 /
自动打补丁）其实和"页面"没关系。只要有地方读文本、能分层失败，它就成立。
CLI 域把 `ActionKind.RUN` 映射成进程执行，断言打在 `stdout + stderr` 上。

刻意**不做桌面 GUI**（见 README「域」一节）：
桌面控件树比 DOM 脆一个数量级，靠截图+坐标的自动化在"稳定地把事做对"这件事上
达不到本项目的底线。与其做一个会骗人的域，不如把边界写清楚。

和浏览器域的三处差别：

| | 浏览器域 | CLI 域 |
|---|---|---|
| 步骤 | goto/fill/click… | 只有 `run`（命令模板） |
| 读文本 | 页面 DOM | 进程输出 |
| 写操作查重 | `precheck` 断言（找元素） | `precheck_command`（跑一条命令，返回 0 表示已存在） |
"""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .executor.driver import RunResult, StepResult, render
from .executor.validator import evaluate
from .schema.models import Skill, Step
from .schema.types import ActionKind

DOMAIN = "cli"
DEFAULT_TIMEOUT_S = 60


class OutputPage:
    """把进程输出伪装成 PageAdapter，好让 `validator.evaluate` 直接复用。

    只实现断言真正会用到的那几个方法 —— 其余一律明确不支持，
    免得有人以为 CLI 域也能做元素定位。
    """

    def __init__(self, output: str, cwd: str = "") -> None:
        self._output = output
        self._cwd = cwd

    async def text(self) -> str:
        return self._output

    async def url(self) -> str:
        return self._cwd

    async def exists(self, locator: Any) -> bool:
        raise NotImplementedError("CLI 域没有元素概念：请改用 run + 断言输出")

    async def count(self, locator: Any) -> int:
        raise NotImplementedError("CLI 域没有元素概念")

    async def goto(self, url: str) -> None:
        raise NotImplementedError("CLI 域不支持 goto")

    async def fill(self, locator: Any, value: str) -> None:
        raise NotImplementedError("CLI 域不支持 fill")

    async def click(self, locator: Any) -> None:
        raise NotImplementedError("CLI 域不支持 click")

    async def fingerprints(self) -> list[Any]:
        return []


@dataclass
class CommandOutcome:
    returncode: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


async def run_command(
    command: str,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> CommandOutcome:
    """跑一条命令，把 stdout 和 stderr 合起来收（顺序即真实发生顺序）。"""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        return CommandOutcome(returncode=-1, output=f"（超时 {timeout_s}s 被杀）", timed_out=True)
    encoding = "utf-8" if sys.platform != "win32" else "utf-8"
    return CommandOutcome(returncode=proc.returncode or 0, output=raw.decode(encoding, "replace"))


async def run_cli_skill(
    skill: Skill,
    params: Mapping[str, Any],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    run_id: str = "cli",
) -> RunResult:
    """按顺序跑步骤，任何一步失败就停（后面的步骤依赖前面的产物）。"""
    result = RunResult(
        run_id=run_id,
        skill_id=skill.skill_id,
        version=skill.version,
        status="success",
        params=dict(params),
    )
    merged_env = {**os.environ, **(env or {})}
    # idempotency_key 里的 {{flow_run_id}} / {{step_id}} 也要能渲染
    render_params = {**params, "flow_run_id": run_id}

    for step in skill.steps:
        step_result = StepResult(step_id=step.step_id, action=step.action.value)

        if step.action is not ActionKind.RUN:
            step_result.status = "failed"
            step_result.failure_layer = "step"
            step_result.detail = f"CLI 域只支持 run 动作，不支持 {step.action.value}"
            step_result.detail += "（想用浏览器动作请把 skill.domain 设成 browser）"
            result.steps.append(step_result)
            result.status = "failed"
            break

        try:
            command = render(str(step.value or ""), render_params)
        except KeyError as exc:
            step_result.status = "failed"
            step_result.failure_layer = "param"
            step_result.detail = f"命令模板缺参数 {exc.args[0]!r}"
            result.steps.append(step_result)
            result.status = "failed"
            break

        # 幂等门：写操作必须能查重，查不了就不许执行（和浏览器域同一条底线）
        if step.is_write:
            if not step.precheck_command:
                step_result.status = "blocked"
                step_result.failure_layer = "precheck"
                step_result.detail = (
                    "写操作缺 precheck_command：无法判断这一步是否已经做过，拒绝执行"
                )
                result.steps.append(step_result)
                result.status = "failed"
                break
            try:
                check_cmd = render(str(step.precheck_command), render_params)
            except KeyError as exc:
                step_result.status = "failed"
                step_result.failure_layer = "param"
                step_result.detail = f"precheck_command 缺参数 {exc.args[0]!r}"
                result.steps.append(step_result)
                result.status = "failed"
                break
            check = await run_command(check_cmd, cwd=cwd, env=merged_env, timeout_s=timeout_s)
            if check.ok:
                step_result.status = "skipped"
                step_result.detail = f"precheck 命中（返回 0），跳过写操作：{check.output.strip()[:120]}"
                result.steps.append(step_result)
                continue

        outcome = await run_command(command, cwd=cwd, env=merged_env, timeout_s=timeout_s)
        page = OutputPage(outcome.output, str(cwd or ""))

        if outcome.timed_out:
            step_result.status = "failed"
            step_result.failure_layer = "step"
            step_result.detail = outcome.output
            result.steps.append(step_result)
            result.status = "failed"
            break

        if outcome.returncode != 0:
            step_result.status = "failed"
            step_result.failure_layer = "step"
            step_result.detail = (
                f"命令返回 {outcome.returncode}：{outcome.output.strip()[:300]}"
            )
            result.steps.append(step_result)
            result.status = "failed"
            break

        # 断言打在输出上（和浏览器域同一套算子、同一套分级）
        if step.assertion is not None:
            verdict = await evaluate(step.assertion, page)
            if not verdict.passed:
                step_result.business_failures.append(
                    f"{verdict.assertion_id}: {verdict.detail}"
                )
        if step.technical_assertion is not None:
            verdict = await evaluate(step.technical_assertion, page)
            if not verdict.passed:
                step_result.technical_failures.append(
                    f"{verdict.assertion_id}: {verdict.detail}"
                )

        if step_result.business_failures:
            step_result.status = "failed"
            step_result.failure_layer = "assertion"
            step_result.detail = step_result.business_failures[0]
            result.steps.append(step_result)
            result.status = "failed"
            break

        step_result.detail = outcome.output.strip()[:200]
        result.steps.append(step_result)

    return result


def make_runner(**kwargs: Any):
    """给 golden / batch 这类只认 `runner(skill, params) -> RunResult` 的地方用。"""

    async def runner(skill: Skill, params: Mapping[str, Any]) -> RunResult:
        return await run_cli_skill(skill, params, **kwargs)

    return runner


def command_of(step: Step) -> str:
    return str(step.value or "")


def preview(skill: Skill) -> list[str]:
    """人看的一行一步，用来在跑之前确认。"""
    lines = []
    for step in skill.steps:
        flag = "  [写]" if step.is_write else ""
        check = f"  precheck: {step.precheck_command}" if step.precheck_command else ""
        lines.append(f"  {step.step_id}. $ {step.value}{flag}")
        if check:
            lines.append(f"      {check.strip()}")
    return lines


__all__ = [
    "CommandOutcome",
    "DOMAIN",
    "OutputPage",
    "command_of",
    "make_runner",
    "preview",
    "run_cli_skill",
    "run_command",
]

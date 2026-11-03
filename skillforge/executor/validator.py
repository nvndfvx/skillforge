"""断言求值器：把 Assertion 变成一个可执行的判据。

business 失败 → 技能/流程问题；
technical 失败 → 只是环境探针响了一声，**不判技能死**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..schema.models import Assertion
from ..schema.types import AssertionKind
from .adapter import PageAdapter

SUPPORTED_OPERATORS = (
    "contains",
    "equals",
    "exists",
    "visible",
    "count_eq",
    "url_match",
    "entity_exists",
)


@dataclass
class AssertionResult:
    assertion_id: str = ""
    kind: str = ""
    passed: bool = False
    detail: str = ""
    unsupported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "kind": self.kind,
            "passed": self.passed,
            "detail": self.detail,
            "unsupported": self.unsupported,
        }


def _result(assertion: Assertion, passed: bool, detail: str = "", unsupported: bool = False):
    return AssertionResult(
        assertion_id=assertion.assertion_id,
        kind=assertion.kind.value,
        passed=passed,
        detail=detail,
        unsupported=unsupported,
    )


async def evaluate(assertion: Assertion, page: PageAdapter) -> AssertionResult:
    if assertion is None:
        return AssertionResult(passed=True, detail="无断言")
    op = assertion.operator
    expected = assertion.expected

    if op not in SUPPORTED_OPERATORS:
        return _result(
            assertion, False, f"不支持的算子「{op}」（可能是 attr_eq，需要属性读取接口）", True
        )

    try:
        if op == "contains":
            text = await page.text()
            return _result(assertion, str(expected) in text, f"页面文本{'包含' if str(expected) in text else '不包含'}「{expected}」")

        if op == "equals":
            text = (await page.text()).strip()
            ok = text == str(expected)
            return _result(assertion, ok, f"页面文本{'等于' if ok else '不等于'}期望值")

        if op in ("exists", "visible"):
            ok = await page.exists(assertion.target)
            return _result(assertion, ok, f"元素{'存在' if ok else '不存在'}")

        if op == "count_eq":
            actual = await page.count(assertion.target)
            ok = actual == int(expected)
            return _result(assertion, ok, f"数量 {actual}（期望 {expected}）")

        if op == "url_match":
            current = await page.url()
            ok = str(expected) in current
            return _result(assertion, ok, f"当前 URL={current}")

        if op == "entity_exists":
            text = await page.text()
            pattern = str(expected)
            hit = re.search(pattern, text)
            ok = hit is not None
            return _result(
                assertion,
                ok,
                f"业务回执{'命中' if ok else '未命中'}：/{pattern}/"
                + (f" → {hit.group(0)}" if hit else ""),
            )

        return _result(assertion, False, f"算子「{op}」未实现", True)
    except Exception as exc:  # noqa: BLE001
        return _result(assertion, False, f"断言求值异常：{type(exc).__name__}: {exc}")


def is_business(assertion: Assertion) -> bool:
    return assertion is not None and assertion.kind is AssertionKind.BUSINESS


__all__ = ["AssertionResult", "SUPPORTED_OPERATORS", "evaluate", "is_business"]

"""从参数生成候选 Golden 用例。

**重要**：这里产出的是「候选」，不是判据。

Golden Set 的定义是"这些用例都应当通过"。乱造会污染闸——
所以生成的每一条都标 `origin="manual_edge"`，并在 CLI 里提示必须人工确认。
它解决的是「3 条太少、人又懒得想」的问题，不是「自动获得真值」。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Sequence

from ..schema.models import GoldenCase, Parameter, Skill


def _int_variants(example: Any) -> list[Any]:
    try:
        value = int(str(example))
    except (TypeError, ValueError):
        return []
    out = []
    for candidate in (value + 1, value - 1, value * 2, 0, value + 10):
        if candidate != value and candidate >= 0:
            out.append(candidate)
    return out


def _date_variants(example: Any) -> list[Any]:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            day = _dt.datetime.strptime(str(example), fmt).date()
        except (TypeError, ValueError):
            continue
        return [
            (day - _dt.timedelta(days=1)).strftime(fmt),
            (day + _dt.timedelta(days=1)).strftime(fmt),
            (day - _dt.timedelta(days=7)).strftime(fmt),
            (day + _dt.timedelta(days=7)).strftime(fmt),
        ]
    return []


def _string_variants(example: Any) -> list[Any]:
    text = str(example)
    if not text:
        return []
    candidates = [text + "A", "A" + text, text + "甲", text + text]
    if len(text) > 2:
        candidates.append(text[1:])
    # 去重且排除原值
    return [c for c in dict.fromkeys(candidates) if c != text]


def _enum_variants(param: Parameter) -> list[Any]:
    values = param.constraints.get("values") or param.constraints.get("enum") or []
    return [v for v in values if v != param.example]


def variants(param: Parameter) -> list[Any]:
    kind = (param.type or "string").lower()
    if kind == "int":
        return _int_variants(param.example)
    if kind == "date":
        return _date_variants(param.example)
    if kind == "enum":
        return _enum_variants(param)
    return _string_variants(param.example)


def suggest_cases(
    skill: Skill,
    *,
    base_params: dict[str, Any] | None = None,
    limit: int = 12,
    prefix: str = "suggest",
) -> list[GoldenCase]:
    """生成 1 条基准 + 若干「单参数扰动」候选，最多 limit 条。"""
    base: dict[str, Any] = dict(base_params or {})
    for param in skill.parameters:
        if param.name not in base and param.example is not None:
            base[param.name] = param.example

    cases: list[GoldenCase] = [
        GoldenCase(
            case_id=f"{prefix}-base",
            params=dict(base),
            origin="manual_edge",
            created_from_version=skill.version,
        )
    ]
    seen = {_key(base)}

    for param in skill.parameters:
        for value in variants(param):
            params = dict(base)
            params[param.name] = value
            key = _key(params)
            if key in seen:
                continue
            seen.add(key)
            cases.append(
                GoldenCase(
                    case_id=f"{prefix}-{param.name}-{len(cases)}",
                    params=params,
                    origin="manual_edge",
                    created_from_version=skill.version,
                )
            )
            if len(cases) >= limit:
                return cases
    return cases


def _key(params: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in params.items()))


__all__ = ["suggest_cases", "variants"]

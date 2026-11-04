"""JSON 反序列化：把 to_dict() 的产物读回数据类。

持久化（版本库、Golden Set、Trace 文件）都需要它。
刻意写成独立模块，避免 models.py 里一堆 from_dict 干扰阅读。
"""
from __future__ import annotations

from typing import Any

from .models import (
    Assertion,
    EntityCheck,
    GoldenCase,
    Parameter,
    Precondition,
    Skill,
    Step,
)
from .types import (
    ActionKind,
    AnchorKind,
    AssertionKind,
    ElementFingerprint,
    Locator,
    RawAction,
    RawTrace,
    WriteEffect,
)


def _get(data: Any, key: str, default: Any = None) -> Any:
    if isinstance(data, dict):
        return data.get(key, default)
    return default


def locator_from_dict(data: Any) -> Locator:
    if not data:
        return Locator()
    return Locator(
        role=_get(data, "role"),
        name=_get(data, "name"),
        css=_get(data, "css"),
    )


def fingerprint_from_dict(data: Any) -> ElementFingerprint | None:
    if not data:
        return None
    bbox = _get(data, "bbox_rel") or (0.0, 0.0, 0.0, 0.0)
    return ElementFingerprint(
        role=str(_get(data, "role") or ""),
        accessible_name=str(_get(data, "accessible_name") or ""),
        text_norm=str(_get(data, "text_norm") or ""),
        attrs_subset=dict(_get(data, "attrs_subset") or {}),
        neighbor_text=str(_get(data, "neighbor_text") or ""),
        bbox_rel=tuple(bbox),  # type: ignore[arg-type]
        frame_url=str(_get(data, "frame_url") or ""),
        frame_path=str(_get(data, "frame_path") or ""),
        captured_version=str(_get(data, "captured_version") or ""),
    )


def assertion_from_dict(data: Any) -> Assertion | None:
    if not data:
        return None
    raw_target = _get(data, "target")
    return Assertion(
        assertion_id=str(_get(data, "assertion_id") or ""),
        kind=AssertionKind(str(_get(data, "kind") or "technical")),
        anchor=AnchorKind(str(_get(data, "anchor") or "ui_text")),
        operator=str(_get(data, "operator") or "contains"),
        # None 和「空 Locator」不是一回事：technical 断言本来就没有 target，
        # 在 from_dict 里把它变成 Locator() 会让 JSON 往返不相等。
        target=locator_from_dict(raw_target) if raw_target else None,
        expected=_get(data, "expected"),
        timeout_ms=int(_get(data, "timeout_ms") or 5000),
        confirmed_by=str(_get(data, "confirmed_by") or "llm_proposed"),
        source=str(_get(data, "source") or "observed"),
        last_verified=_get(data, "last_verified"),
        risk_note=_get(data, "risk_note"),
    )


def parameter_from_dict(data: Any) -> Parameter:
    return Parameter(
        name=str(_get(data, "name") or ""),
        type=str(_get(data, "type") or "string"),
        required=bool(_get(data, "required", True)),
        source=str(_get(data, "source") or "manual"),
        example=_get(data, "example"),
        constraints=dict(_get(data, "constraints") or {}),
    )


def precondition_from_dict(data: Any) -> Precondition:
    return Precondition(
        kind=str(_get(data, "kind") or "on_page"),
        locator_or_path=str(_get(data, "locator_or_path") or ""),
        check=assertion_from_dict(_get(data, "check")),
    )


def entity_check_from_dict(data: Any) -> EntityCheck | None:
    if not isinstance(data, dict):
        return None
    pattern = str(data.get("pattern") or "").strip()
    param = str(data.get("param") or "").strip()
    if not pattern or not param:
        return None
    return EntityCheck(pattern=pattern, param=param, note=str(data.get("note") or ""))


def step_from_dict(data: Any) -> Step:
    return Step(
        step_id=int(_get(data, "step_id") or 0),
        action=ActionKind(str(_get(data, "action") or "click")),
        target=locator_from_dict(_get(data, "target")),
        tab=int(_get(data, "tab") or 0),
        entity_check=entity_check_from_dict(_get(data, "entity_check")),
        value=_get(data, "value"),
        write_effect=WriteEffect(str(_get(data, "write_effect") or "none")),
        idempotency_key=_get(data, "idempotency_key"),
        precheck=assertion_from_dict(_get(data, "precheck")),
        assertion=assertion_from_dict(_get(data, "assertion")),
        technical_assertion=assertion_from_dict(_get(data, "technical_assertion")),
        fingerprint=fingerprint_from_dict(_get(data, "fingerprint")),
        precheck_command=str(_get(data, "precheck_command") or ""),
        fragile=bool(_get(data, "fragile", False)),
        note=str(_get(data, "note") or ""),
    )


def skill_from_dict(data: Any) -> Skill:
    return Skill(
        skill_id=str(_get(data, "skill_id") or ""),
        name=str(_get(data, "name") or ""),
        description=str(_get(data, "description") or ""),
        version=str(_get(data, "version") or "1.0.0"),
        parameters=[parameter_from_dict(x) for x in (_get(data, "parameters") or [])],
        preconditions=[
            precondition_from_dict(x) for x in (_get(data, "preconditions") or [])
        ],
        steps=[step_from_dict(x) for x in (_get(data, "steps") or [])],
        meta=dict(_get(data, "meta") or {}),
        domain=str(_get(data, "domain") or "browser"),
        state=str(_get(data, "state") or "draft"),
    )


def golden_case_from_dict(data: Any) -> GoldenCase:
    return GoldenCase(
        case_id=str(_get(data, "case_id") or ""),
        params=dict(_get(data, "params") or {}),
        extra_assertions=[
            a for a in (assertion_from_dict(x) for x in (_get(data, "extra_assertions") or [])) if a
        ],
        origin=str(_get(data, "origin") or "demo"),
        created_from_version=str(_get(data, "created_from_version") or "1.0.0"),
        last_pass_version=_get(data, "last_pass_version"),
    )


def raw_action_from_dict(data: Any) -> RawAction:
    return RawAction(
        seq=int(_get(data, "seq") or 0),
        action=str(_get(data, "action") or ""),
        role=str(_get(data, "role") or ""),
        name=str(_get(data, "name") or ""),
        value=_get(data, "value"),
        selector=str(_get(data, "selector") or ""),
        url=str(_get(data, "url") or ""),
        frame_url=str(_get(data, "frame_url") or ""),
        fingerprint=fingerprint_from_dict(_get(data, "fingerprint")),
        before_text=str(_get(data, "before_text") or ""),
        after_text=str(_get(data, "after_text") or ""),
        pause_before_ms=int(_get(data, "pause_before_ms") or 0),
        t_ms=int(_get(data, "t_ms") or 0),
        source=str(_get(data, "source") or "event"),
    )


def raw_trace_from_dict(data: Any) -> RawTrace:
    return RawTrace(
        trace_id=str(_get(data, "trace_id") or ""),
        started_at=str(_get(data, "started_at") or ""),
        url=str(_get(data, "url") or ""),
        actions=[raw_action_from_dict(x) for x in (_get(data, "actions") or [])],
        form_snapshots=list(_get(data, "form_snapshots") or []),
        meta=dict(_get(data, "meta") or {}),
    )


__all__ = [
    "assertion_from_dict",
    "fingerprint_from_dict",
    "golden_case_from_dict",
    "locator_from_dict",
    "parameter_from_dict",
    "precondition_from_dict",
    "raw_action_from_dict",
    "raw_trace_from_dict",
    "skill_from_dict",
    "step_from_dict",
]
